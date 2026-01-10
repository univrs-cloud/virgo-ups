import logging
import struct
import subprocess
import time
import smbus

from datetime import datetime
from threading import Thread

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


class SystemPower:
    """Monitors system power status from UPS battery via I2C and button input."""

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

        self.power_source_button = button
        self.power_source_button.when_pressed = self._running_from_grid
        self.power_source_button.when_blinking = self._running_from_grid_charging
        self.power_source_button.when_released = self._running_from_battery
        self.power_source_button.when_held = self._beeing_held

        # Socket API reference for broadcasting changes
        self._socket_api = None

        # Track previous values to detect changes
        self._previous_capacity = None
        self._previous_voltage = None

        self._read_power_values()
        self.power_source_button.start()

    def set_socket_api(self, socket_api):
        """Set the socket API instance for broadcasting status changes.
        
        Args:
            socket_api: UnixSocketApi instance
        """
        self._socket_api = socket_api

    def _broadcast_if_changed(self, force=False):
        """Broadcast status if values have changed.
        
        Args:
            force: If True, broadcast even if no changes detected
        """
        if self._socket_api is None:
            return

        capacity_changed = self._previous_capacity is None or self.capacity != self._previous_capacity
        voltage_changed = self._previous_voltage is None or abs(self.voltage - self._previous_voltage) > 0.01  # 0.01V threshold

        if force or capacity_changed or voltage_changed:
            self._socket_api.broadcast_status()
            self._previous_capacity = self.capacity
            self._previous_voltage = self.voltage

    def _read_voltage(self):
        """Read battery voltage from I2C device.
        
        Returns:
            float: Battery voltage in volts
        """
        read = self.bus.read_word_data(I2C_BATTERY_ADDRESS, REGISTER_VOLTAGE)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        self.voltage = swapped * VOLTAGE_SCALE_FACTOR
        return self.voltage

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
            self.is_charging = False
        self.set_power_source(power_source, log_change=True)

    def _beeing_held(self):
        """Callback for when button is held (currently unused)."""
        pass

    def _running_from_grid_charging(self):
        """Callback for when power source is grid with charging (blinking detected)."""
        old_charging = self.is_charging
        self.is_charging = True
        self.set_power_source(GRID_POWER, log_change=True)
        # Broadcast if charging state changed
        if old_charging != self.is_charging and self._socket_api:
            self._socket_api.broadcast_status()

    def _running_from_grid(self):
        """Callback for when power source switches to grid."""
        old_charging = self.is_charging
        self.is_charging = False
        self.set_power_source(GRID_POWER, log_change=True)
        # Broadcast if charging state changed
        if old_charging != self.is_charging and self._socket_api:
            self._socket_api.broadcast_status()

    def _running_from_battery(self):
        """Callback for when power source switches to battery."""
        old_charging = self.is_charging
        self.is_charging = False
        self._read_power_values()
        self.set_power_source(BATTERY_POWER, log_change=True)
        # Broadcast charging state change
        if old_charging != self.is_charging and self._socket_api:
            self._socket_api.broadcast_status()
        if self.running and self.has_critical_battery_power():
            self.shutdown()

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
        
        Args:
            power_source: Power source constant (GRID_POWER or BATTERY_POWER)
            log_change: If True, log when power source changes
        """
        old_power_source = self.power_source
        if log_change and self.power_source and self.power_source != power_source:
            logging.info(f"Power source switched from {self.power_source} to {power_source}")

        self.power_source = power_source
        self.is_running_from_battery = self.power_source == BATTERY_POWER
        
        # Broadcast if power source changed
        if old_power_source and old_power_source != power_source and self._socket_api:
            self._socket_api.broadcast_status()

    def status(self):
        """Get human-readable status string.
        
        Returns:
            str: Formatted status message
        """
        charging_status = " (charging)" if self.is_charging else ""
        return f"Battery: {self.capacity:2}% ({self.voltage:4.2f}V), power: {self.power_source}{charging_status}"

    def status_dict(self, refresh: bool = True) -> dict:
        """Get status as dictionary.
        
        Args:
            refresh: If True, refresh values from hardware before returning
            
        Returns:
            dict: Status information including capacity, voltage, and power source
        """
        if refresh:
            self._read_power_values()
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
        return self.is_running_from_battery and (self.capacity <= self.low_capacity_threshold)

    def shutdown(self):
        """Initiate system shutdown due to low battery."""
        if self.has_issued_shutdown:
            return
        try:
            logging.critical(
                f"Issuing shutdown command (Battery: {self.capacity:2}%, threshold: {self.low_capacity_threshold:2}%)"
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
            # Broadcast if capacity or voltage changed
            self._broadcast_if_changed()
            
            min_interval = 1 if self.is_running_from_battery else max(10, max_interval / 2.0)
            interval = scaled_value(
                self.capacity, self.low_capacity_threshold, self.low_capacity_threshold + 15, min_interval, max_interval
            )
            uptime_minutes = uptime.total_seconds() / 60.0
            if interval > 45:
                logging.info(f"[{uptime_minutes:5.0f}] {self.status()}")
            else:
                logging.info(f"[{uptime_minutes:7.2f}] {self.status()}")

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
            # Broadcast if capacity or voltage changed
            self._broadcast_if_changed()
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
