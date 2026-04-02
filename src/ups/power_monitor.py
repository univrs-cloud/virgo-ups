import logging
import struct
import subprocess
import time
import smbus

from datetime import datetime
from threading import Thread, Lock

from gpiozero import OutputDevice

from .settings import is_development

# Power source constants
GRID_POWER = "grid"
BATTERY_POWER = "battery"

# I2C bus constants
I2C_BUS_PORT = 1  # 0 = /dev/i2c-0 (port I2C0), 1 = /dev/i2c-1 (port I2C1)
I2C_BATTERY_ADDRESS = 0x36  # Battery fuel gauge I2C address

# Register addresses for battery readings
REGISTER_VOLTAGE = 2  # Voltage register
REGISTER_CAPACITY = 4  # Capacity register

# Calculation constants
VOLTAGE_SCALE_FACTOR = 1.25 / 1000 / 16  # Voltage calculation scaling
CAPACITY_SCALE_FACTOR = 256  # Capacity calculation scaling (from 256 steps to percentage)

# UPS V2.5+ software charge control (Geekworm): BCM GPIO 16 (header pin 36).
# Drive HIGH to enable charging, LOW to disable (per Geekworm hardware notes).
CHARGE_CONTROL_GPIO = 16
CHARGE_ENABLE_BELOW_PCT = 90.0
CHARGE_DISABLE_ABOVE_PCT = 97.0


def software_charge_enabled_for_soc(
    capacity_pct,
    previous_enabled,
    enable_below_pct,
    disable_above_pct,
):
    """Decide whether software charge control should enable charging.

    Args:
        capacity_pct: Current state of charge (percent).
        previous_enabled: Last decision when SoC was in the hysteresis band,
            or None if there is no prior decision (defaults to enabling).
        enable_below_pct: Enable charging when SoC is strictly below this.
        disable_above_pct: Disable charging when SoC is strictly above this.

    Returns:
        bool: True to enable charging, False to disable.
    """
    if capacity_pct < enable_below_pct:
        return True
    if capacity_pct > disable_above_pct:
        return False
    if previous_enabled is None:
        return True
    return previous_enabled


class SystemPower:
    """Monitors system power status from UPS battery via I2C and button input.

    On UPS V2.5+ hardware, toggles BCM GPIO 16 to enable/disable charging from
    state of charge (enable below 90%, disable above 99%, with hysteresis between).
    """

    def __init__(self, button):
        """Initialize power monitor with button input for power source detection.
        
        Args:
            button: Button instance that detects power source state (pressed = grid, released = battery)
        """
        self.bus = smbus.SMBus(I2C_BUS_PORT)

        self.voltage = None
        self.capacity = None
        self.capacity_float = None
        self.power_source = None
        self.is_running_from_battery = None
        self.is_charging = False
        self.has_read_errors = False
        self.running = False
        self.low_capacity_threshold = 50
        self.has_issued_shutdown = False
        self.issued_shutdown_timestamp = None

        # Add lock to prevent race conditions during power source transitions
        self._state_lock = Lock()

        self.power_source_button = button
        self.power_source_button.when_pressed = self._running_from_grid
        self.power_source_button.when_released = self._running_from_battery
        self.power_source_button.when_held = self._beeing_held

        # Socket API reference for broadcasting changes
        self._socket_api = None

        # Guard to prevent recursive broadcasts
        self._broadcasting = False

        # Track previous values to detect changes
        self._previous_capacity = None
        self._previous_voltage = None
        self._previous_is_charging = None

        # Charging detection parameters
        self._charging_detection_samples = 3
        self._charging_detection_interval = 5  # seconds
        self._last_charging_check_time = None
        self._charging_check_interval = 20  # Check charging every 20 seconds when on grid
        self._charging_detection_in_progress = False
        self._charging_detection_lock = Lock()

        # Shutdown safety parameters
        self._shutdown_confirmation_required = True  # Require power source confirmation before shutdown
        self._shutdown_grace_period = 5  # Seconds to wait and re-check before actual shutdown

        # UPS software charge enable (GPIO 16); None if unavailable (e.g. dev host).
        self._charge_pin = None
        self._charge_control_desired = None
        self._init_charge_control_gpio()

        self._read_power_values()
        if not self.has_read_errors:
            self._sync_charge_control_gpio()
        self.power_source_button.start()

    def _init_charge_control_gpio(self):
        """Set up UPS GPIO 16 charge control when running on real hardware."""
        if is_development():
            return
        try:
            # Default HIGH until first successful SoC-based sync (enable charging).
            self._charge_pin = OutputDevice(CHARGE_CONTROL_GPIO, initial_value=True)
            logging.info(
                "UPS software charge control initialized (GPIO %s HIGH = charging enabled)",
                CHARGE_CONTROL_GPIO,
            )
        except Exception as e:
            logging.warning("UPS charge control GPIO unavailable: %s", repr(e))

    def _sync_charge_control_gpio(self):
        """Drive GPIO 16 from SoC using software_charge_enabled_for_soc."""
        if self._charge_pin is None:
            return
        if self.has_read_errors:
            return
        c = self.capacity_float
        if c is None:
            return
        want_charge = software_charge_enabled_for_soc(
            c,
            self._charge_control_desired,
            CHARGE_ENABLE_BELOW_PCT,
            CHARGE_DISABLE_ABOVE_PCT,
        )
        self._charge_control_desired = want_charge
        if want_charge:
            if not self._charge_pin.is_active:
                self._charge_pin.on()
                logging.info("UPS charging enabled via GPIO %s (SoC %.1f%%)", CHARGE_CONTROL_GPIO, c)
        else:
            if self._charge_pin.is_active:
                self._charge_pin.off()
                logging.info("UPS charging disabled via GPIO %s (SoC %.1f%%)", CHARGE_CONTROL_GPIO, c)

    def set_socket_api(self, socket_api):
        """Set the socket API instance for broadcasting status changes.
        
        Args:
            socket_api: UnixSocketApi instance
        """
        self._socket_api = socket_api

    def _update_previous_values(self):
        """Update previous capacity, voltage, and charging state to current values.
        
        This should be called after broadcasting status to ensure future changes
        are detected correctly, especially after direct broadcasts from plug/unplug
        events that refresh values via status_dict().
        """
        self._previous_capacity = self.capacity
        self._previous_voltage = self.voltage
        self._previous_is_charging = self.is_charging

    def _broadcast_status(self, force=False):
        """Broadcast status to connected clients if values changed.
        
        Prevents recursive broadcasts: if a broadcast is already in progress
        (e.g., status_dict refresh triggers set_power_source which triggers
        another broadcast), the nested call is skipped.
        
        Args:
            force: If True, broadcast even if no changes detected
        """
        if self._socket_api is None:
            return

        # Prevent recursive broadcasts
        if self._broadcasting:
            return

        capacity_changed = self._previous_capacity is None or self.capacity != self._previous_capacity
        voltage_changed = self._previous_voltage is None or abs(self.voltage - self._previous_voltage) > 0.01
        charging_changed = self._previous_is_charging is None or self.is_charging != self._previous_is_charging

        if force or capacity_changed or voltage_changed or charging_changed:
            self._broadcasting = True
            try:
                self._socket_api.broadcast_status()
                self._update_previous_values()
            finally:
                self._broadcasting = False

    def _read_voltage(self):
        """Read battery voltage from I2C device.
        
        Returns:
            float: Battery voltage in volts
        """
        read = self.bus.read_word_data(I2C_BATTERY_ADDRESS, REGISTER_VOLTAGE)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        self.voltage = swapped * VOLTAGE_SCALE_FACTOR
        return self.voltage

    def _detect_charging(self, samples=3, interval=5):
        """Detect if battery is charging based on capacity trend.
        
        Takes multiple capacity readings at fixed intervals to detect if capacity
        is increasing (charging) or stable/decreasing (not charging).
        
        Args:
            samples: Number of capacity samples to take (default: 3)
            interval: Time interval between samples in seconds (default: 5)
            
        Returns:
            bool: True if charging (capacity increasing), False otherwise
        """
        readings = []
        for _ in range(samples):
            self._read_capacity()  # Update capacity_float
            readings.append(self.capacity_float)  # Use float for precision
            if _ < samples - 1:  # Don't sleep after last sample
                time.sleep(interval)
        
        if len(readings) < samples:
            return None
        
        delta = readings[-1] - readings[0]
        # If capacity increased by at least 0.1%, consider it charging
        return delta > 0.1

    def _check_charging_status_background(self):
        """Background thread to detect charging status (blocks for detection period)."""
        try:
            # Detect charging based on voltage trend (this will block for samples * interval seconds)
            charging_state = self._detect_charging(
                samples=self._charging_detection_samples,
                interval=self._charging_detection_interval
            )
            if charging_state is not None:
                with self._charging_detection_lock:
                    old_charging = self.is_charging
                    if old_charging != charging_state:
                        self.is_charging = charging_state
                        self._last_charging_check_time = datetime.utcnow()
                
                # Broadcast if charging state changed (outside lock)
                if old_charging != charging_state:
                    if charging_state:
                        logging.info(f"Charging detected (capacity increasing)")
                    else:
                        logging.info(f"Charging stopped (capacity stable/decreasing)")
                    self._broadcast_status(force=True)
        except Exception as e:
            logging.warning(f"Error in charging detection: {e}")
        finally:
            with self._charging_detection_lock:
                self._charging_detection_in_progress = False

    def _check_charging_status(self):
        """Check charging status when on grid power (non-blocking - starts background thread)."""
        with self._state_lock:
            current_power_source = self.power_source
        
        if current_power_source != GRID_POWER:
            with self._charging_detection_lock:
                self._charging_detection_in_progress = False
            return
        
        now = datetime.utcnow()
        should_check = (
            not self._charging_detection_in_progress and
            (self._last_charging_check_time is None or
             (now - self._last_charging_check_time).total_seconds() >= self._charging_check_interval)
        )
        if should_check:
            with self._charging_detection_lock:
                if not self._charging_detection_in_progress:
                    self._charging_detection_in_progress = True
                    # Start background thread to detect charging (non-blocking)
                    thread = Thread(target=self._check_charging_status_background, daemon=True)
                    thread.start()

    def _read_capacity(self):
        """Read battery capacity from I2C device.
        
        Returns:
            int: Battery capacity as percentage (0-100)
        """
        read = self.bus.read_word_data(I2C_BATTERY_ADDRESS, REGISTER_CAPACITY)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        self.capacity_float = swapped / CAPACITY_SCALE_FACTOR
        self.capacity = int(self.capacity_float)
        return self.capacity

    def _read_power_source(self):
        """Read power source from button state."""
        power_source = GRID_POWER if self.power_source_button.is_pressed else BATTERY_POWER
        # If switching to battery, charging is definitely False
        if power_source == BATTERY_POWER:
            with self._charging_detection_lock:
                self.is_charging = False
                self._last_charging_check_time = None
        self.set_power_source(power_source, log_change=True)

    def _beeing_held(self):
        """Callback for when button is held (currently unused)."""
        pass

    def _running_from_grid(self):
        """Callback for when power source switches to grid.
        
        NOTE: This is called from the button's GPIO callback thread.
        Must not block or do heavy I/O while holding locks, otherwise
        subsequent GPIO edge events can be lost.
        """
        with self._state_lock:
            self.is_charging = False
            # Cancel any pending shutdown when switching to grid power
            if self.has_issued_shutdown:
                logging.warning("Shutdown cancelled - grid power restored")
                self.has_issued_shutdown = False
                self.issued_shutdown_timestamp = None
            self.set_power_source(GRID_POWER, log_change=True)

        # Broadcast OUTSIDE the state lock to avoid blocking GPIO callbacks
        self._broadcast_status(force=True)

    def _running_from_battery(self):
        """Callback for when power source switches to battery.
        
        NOTE: This is called from the button's GPIO callback thread.
        Must not block or do heavy I/O while holding locks.
        """
        with self._state_lock:
            self.is_charging = False
            self.set_power_source(BATTERY_POWER, log_change=True)

        # Read I2C values and broadcast OUTSIDE the state lock
        self._read_voltage()
        self._read_capacity()
        self._broadcast_status(force=True)

        # Check for critical battery but don't shutdown immediately from callback
        # Let the main monitoring loop handle it with proper checks
        if self.running and self.has_critical_battery_power():
            logging.warning(
                f"Critical battery level detected on switch to battery: {self.capacity}% "
                f"(threshold: {self.low_capacity_threshold}%)"
            )

    def _read_power_values(self):
        """Read all power-related values from I2C and button."""
        readers = [
            {"name": "voltage", "func": self._read_voltage},
            {"name": "capacity", "func": self._read_capacity},
            {"name": "power source", "func": self._read_power_source},
        ]
        has_errors = False
        for reader in readers:
            label = reader.get("name")
            reader_func = reader.get("func")
            try:
                reader_func()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                has_errors = True
                logging.warning(f"Failed to read {label}: {repr(e)}")
        self.has_read_errors = has_errors

    def set_power_source(self, power_source, log_change=False):
        """Set the power source and update internal state.
        
        NOTE: Does NOT broadcast — callers are responsible for broadcasting
        after releasing any locks. This prevents recursive broadcast chains
        and avoids holding locks during I/O.
        
        Args:
            power_source: Power source constant (GRID_POWER or BATTERY_POWER)
            log_change: If True, log when power source changes
        """
        if log_change and self.power_source and self.power_source != power_source:
            logging.info(f"Power source switched from {self.power_source} to {power_source}")

        self.power_source = power_source
        self.is_running_from_battery = self.power_source == BATTERY_POWER

    def status(self):
        """Get human-readable status string.
        
        Returns:
            str: Formatted status message
        """
        charging_status = " (charging)" if self.is_charging else " (not charging)"
        return f"Battery: {self.capacity:2}% ({self.voltage:4.2f}V), power: {self.power_source}{charging_status}"

    def status_dict(self, refresh: bool = True) -> dict:
        """Get status as dictionary.
        
        Args:
            refresh: If True, refresh voltage and capacity from hardware.
                     Power source is NOT re-read here to avoid triggering
                     set_power_source -> broadcast cycles during a broadcast.
            
        Returns:
            dict: Status information including capacity, voltage, and power source
        """
        if refresh:
            # Only refresh voltage and capacity — NOT power source.
            # Power source is event-driven (button callbacks) and re-reading
            # it here caused recursive broadcast chains:
            #   broadcast -> status_dict -> _read_power_source -> set_power_source -> broadcast
            try:
                self._read_voltage()
            except Exception as e:
                logging.warning(f"Failed to read voltage: {repr(e)}")
            try:
                self._read_capacity()
            except Exception as e:
                logging.warning(f"Failed to read capacity: {repr(e)}")
        return {
            "capacity": self.capacity_float,
            "voltage": self.voltage,
            "power_source": self.power_source,
            "is_charging": self.is_charging,
            "low_capacity_threshold": self.low_capacity_threshold,
        }

    def has_critical_battery_power(self):
        """Check if battery power is critically low.
        
        Returns:
            bool: True if running on battery and capacity is at or below threshold
        """
        with self._state_lock:
            return self.is_running_from_battery and (self.capacity <= self.low_capacity_threshold)

    def _confirm_shutdown_conditions(self):
        """Verify shutdown conditions with fresh readings and grace period.
        
        This prevents race conditions by:
        1. Re-reading power source state
        2. Waiting a grace period
        3. Re-checking all conditions
        
        Returns:
            bool: True if shutdown should proceed, False otherwise
        """
        logging.warning(
            f"Shutdown condition detected (Battery: {self.capacity}%, threshold: {self.low_capacity_threshold}%). "
            f"Waiting {self._shutdown_grace_period}s to confirm..."
        )
        
        # Wait grace period to allow power state to stabilize
        time.sleep(self._shutdown_grace_period)
        
        # Re-read all values with lock held
        with self._state_lock:
            try:
                # Fresh read of power source
                self._read_power_source()
                # Fresh read of capacity
                self._read_capacity()
                
                # Check conditions again
                still_on_battery = self.is_running_from_battery
                still_critical = self.capacity <= self.low_capacity_threshold
                
                if not still_on_battery:
                    logging.info(
                        f"Shutdown cancelled - now on grid power (Battery: {self.capacity}%)"
                    )
                    return False
                
                if not still_critical:
                    logging.info(
                        f"Shutdown cancelled - battery recovered (Battery: {self.capacity}%, "
                        f"threshold: {self.low_capacity_threshold}%)"
                    )
                    return False
                
                # All conditions still met
                logging.critical(
                    f"Shutdown conditions confirmed after grace period: "
                    f"Battery: {self.capacity}%, threshold: {self.low_capacity_threshold}%, "
                    f"power source: {self.power_source}"
                )
                return True
                
            except Exception as e:
                logging.error(f"Error during shutdown confirmation: {repr(e)}")
                # If we can't confirm, err on the side of caution and don't shutdown
                return False

    def shutdown(self):
        """Initiate system shutdown due to low battery with confirmation."""
        if self.has_issued_shutdown:
            return
        
        # If confirmation is required, verify conditions before proceeding
        if self._shutdown_confirmation_required:
            if not self._confirm_shutdown_conditions():
                return
        
        try:
            with self._state_lock:
                # Final check with lock held
                if not self.is_running_from_battery:
                    logging.warning(
                        f"Shutdown aborted at last moment - on grid power (Battery: {self.capacity}%)"
                    )
                    return
                
                logging.critical(
                    f"Issuing shutdown command (Battery: {self.capacity:2}%, "
                    f"threshold: {self.low_capacity_threshold:2}%, "
                    f"power source: {self.power_source})"
                )
                
                if is_development():
                    subprocess.check_call(["echo", "shutdown now"], shell=False)
                else:
                    subprocess.check_call(["shutdown", "-h", "now"], shell=False)
                
                self.has_issued_shutdown = True
                self.issued_shutdown_timestamp = datetime.utcnow()
                
        except KeyboardInterrupt:
            self.has_issued_shutdown = False
            self.issued_shutdown_timestamp = None
            raise
        except Exception as e:
            self.has_issued_shutdown = False
            self.issued_shutdown_timestamp = None
            logging.warning(f"Failed to issue shutdown command: {repr(e)}")

    def stop(self):
        """Stop monitoring and clean up resources."""
        self.running = False
        if isinstance(self.power_source_button, Thread):
            self.power_source_button.stop()
        if self._charge_pin is not None:
            try:
                self._charge_pin.close()
            except Exception as e:
                logging.warning("Failed to close charge control GPIO: %s", repr(e))
            self._charge_pin = None

    def monitor_forever(self, max_interval=60):
        """Monitor power status continuously with adaptive polling interval.
        
        Polling interval adapts based on power source and battery level:
        - More frequent when on battery or low capacity
        - Less frequent when on grid power with good capacity
        
        Args:
            max_interval: Maximum polling interval in seconds (default: 60)
        """
        now = datetime.utcnow()
        self.running = True
        while self.running:
            uptime = datetime.utcnow() - now
            self._read_power_values()
            if not self.has_read_errors:
                self._sync_charge_control_gpio()
            # Check charging status when on grid power (periodically)
            self._check_charging_status()
            # Broadcast if capacity or voltage changed
            self._broadcast_status()
            
            min_interval = 1 if self.is_running_from_battery else max(10, max_interval / 2.0)
            interval = scaled_value(
                self.capacity, self.low_capacity_threshold, self.low_capacity_threshold + 15, min_interval, max_interval
            )
            uptime_minutes = uptime.total_seconds() / 60.0
            if interval > 45:
                logging.info(f"[{uptime_minutes:5.0f}] {self.status()}")
            else:
                logging.info(f"[{uptime_minutes:7.2f}] {self.status()}")

            # Check for critical battery with thread-safe check
            if self.running and self.has_critical_battery_power():
                self.shutdown()
            
            time.sleep(interval)

    def log_forever(self, interval=5):
        """Log power status continuously at fixed interval (for development).
        
        Args:
            interval: Polling interval in seconds (default: 5)
        """
        now = datetime.utcnow()
        self.running = True
        while self.running:
            uptime = datetime.utcnow() - now
            self._read_power_values()
            if not self.has_read_errors:
                self._sync_charge_control_gpio()
            # Check charging status when on grid power (periodically)
            self._check_charging_status()
            # Broadcast if capacity or voltage changed
            self._broadcast_status()
            logging.info(f"[{uptime.total_seconds():4.0f}] {self.status()}")
            time.sleep(interval)


def scaled_value(inp_value, inp_min, inp_max, out_min, out_max):
    """Scale a value from one range to another using linear interpolation.
    
    Args:
        inp_value: Input value to scale
        inp_min: Minimum of input range
        inp_max: Maximum of input range
        out_min: Minimum of output range
        out_max: Maximum of output range
        
    Returns:
        float: Scaled value in output range
        
    Raises:
        ValueError: If input or output ranges are invalid
    """
    if inp_min >= inp_max:
        raise ValueError(f"invalid input interval: {inp_min}, {inp_max}")
    if out_min >= out_max:
        raise ValueError(f"invalid output interval: {out_min}, {out_max}")

    inp_value = max(inp_value, inp_min)
    inp_value = min(inp_value, inp_max)

    out_delta = (inp_value - inp_min) * (out_max - out_min) / (inp_max - inp_min)
    return out_min + out_delta
