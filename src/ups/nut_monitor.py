import json
import logging
import subprocess

from threading import Event, Lock

from .power_monitor import BATTERY_POWER, GRID_POWER, NO_UPS_ERROR

UPSC_BINARY = "/usr/bin/upsc"
NUT_HOST = "127.0.0.1"
UPSC_TIMEOUT = 5.0

POLL_INTERVAL = 5.0
PROBE_INTERVAL = 60.0
READ_FAILURES_BEFORE_LOST = 3

ONLINE_FLAG = "OL"
ON_BATTERY_FLAG = "OB"
CHARGING_FLAG = "CHRG"
DISCHARGING_FLAG = "DISCHRG"

STATUS_VARIABLE = "ups.status"
CHARGE_VARIABLE = "battery.charge"
VOLTAGE_VARIABLE = "battery.voltage"
CHARGE_LOW_VARIABLE = "battery.charge.low"

FULL_CAPACITY = 100.0
DEFAULT_LOW_CAPACITY_THRESHOLD = 50


def parse_variables(output):
    """Parse upsc output into a variable mapping.

    Args:
        output: Text as printed by upsc, one "name: value" pair per line

    Returns:
        dict: Variable names mapped to their values
    """
    variables = {}
    for line in output.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            variables[name.strip()] = value.strip()
    return variables


def to_number(value):
    """Convert a NUT variable to a float.

    Args:
        value: Variable value as reported by upsc, or None

    Returns:
        float or None: The numeric value, or None when it is missing or not numeric
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def power_source_from(flags):
    """Map NUT status flags to a power source.

    Args:
        flags: Tokens from ups.status

    Returns:
        str or None: GRID_POWER, BATTERY_POWER, or None when neither is reported
    """
    if ON_BATTERY_FLAG in flags:
        return BATTERY_POWER
    if ONLINE_FLAG in flags:
        return GRID_POWER
    return None


def charging_from(flags, capacity, power_source):
    """Decide whether the battery is charging.

    Args:
        flags: Tokens from ups.status
        capacity: State of charge (percent), or None
        power_source: GRID_POWER, BATTERY_POWER, or None

    Returns:
        bool: True when the battery is charging
    """
    if CHARGING_FLAG in flags:
        return True
    if DISCHARGING_FLAG in flags:
        return False
    if power_source != GRID_POWER or capacity is None:
        return False
    return capacity < FULL_CAPACITY


class NutMonitor:
    """Monitors a USB UPS through NUT and reports it like the GPIO monitor.

    Reports the no-UPS status until NUT answers for a device, so a host whose
    UPS is absent, unplugged, or still enumerating looks the same to clients as
    a host with no UPS hardware at all.
    """

    def __init__(self):
        """Initialize the monitor with no device known yet."""
        self._socket_api = None
        self._device = None
        self._variables = {}
        self._stale = False
        self._failures = 0
        self._lock = Lock()
        self._stopped = Event()
        self._previous_status = None

    def set_socket_api(self, socket_api):
        """Link the socket API used to broadcast status changes.

        Args:
            socket_api: UnixSocketApi instance
        """
        self._socket_api = socket_api

    def stop(self):
        """Stop the monitoring loop."""
        self._stopped.set()

    def status_dict(self) -> dict:
        """Get status as dictionary.

        Returns:
            dict: The same shape the GPIO monitor reports, or the no-UPS status
                  when NUT has no device to read
        """
        with self._lock:
            device = self._device
            variables = dict(self._variables)
            stale = self._stale

        if device is None:
            return {"battery_charge": False, "error": NO_UPS_ERROR}

        flags = variables.get(STATUS_VARIABLE, "").split()
        capacity = to_number(variables.get(CHARGE_VARIABLE))
        power_source = power_source_from(flags)
        threshold = to_number(variables.get(CHARGE_LOW_VARIABLE))
        return {
            "capacity": capacity,
            "voltage": to_number(variables.get(VOLTAGE_VARIABLE)),
            "power_source": power_source,
            "is_charging": charging_from(flags, capacity, power_source),
            "low_capacity_threshold": (
                DEFAULT_LOW_CAPACITY_THRESHOLD if threshold is None else threshold
            ),
            "read_error": stale,
        }

    def status_message(self) -> str:
        """Get the status as the line to send over the socket.

        Returns:
            str: JSON-encoded status
        """
        return json.dumps(self.status_dict())

    def status(self):
        """Get human-readable status string.

        Returns:
            str: Formatted status message
        """
        with self._lock:
            device = self._device
        if device is None:
            return NO_UPS_ERROR
        current = self.status_dict()
        capacity = current["capacity"]
        voltage = current["voltage"]
        capacity_str = f"{capacity:2.0f}%" if capacity is not None else "  ?%"
        voltage_str = f"{voltage:4.2f}V" if voltage is not None else "?V"
        charging_status = " (charging)" if current["is_charging"] else " (not charging)"
        return (
            f"Battery: {capacity_str} ({voltage_str}), "
            f"power: {current['power_source']}{charging_status}"
        )

    def monitor_forever(self):
        """Poll NUT until stopped, broadcasting whenever the status changes."""
        while not self._stopped.is_set():
            interval = self._refresh()
            self._broadcast_if_changed()
            self._stopped.wait(interval)

    def _refresh(self) -> float:
        """Read the current device and update state.

        Returns:
            float: Seconds to wait before reading again
        """
        device, variables = self._read_device()

        with self._lock:
            if variables is not None:
                if self._device != device:
                    logging.info(f"UPS found on NUT device [{device}]")
                self._device = device
                self._variables = variables
                self._stale = False
                self._failures = 0
                return POLL_INTERVAL

            if self._device is None:
                return PROBE_INTERVAL

            self._failures += 1
            if self._failures < READ_FAILURES_BEFORE_LOST:
                self._stale = True
                return POLL_INTERVAL

            logging.info(f"UPS on NUT device [{self._device}] is no longer answering")
            self._device = None
            self._variables = {}
            self._stale = False
            return PROBE_INTERVAL

    def _read_device(self):
        """Find the first NUT device that answers with a status.

        Returns:
            tuple: (device name, variables), or (None, None) when none answers
        """
        for device in self._list_devices():
            variables = self._read_variables(device)
            if variables is not None:
                return device, variables
        return None, None

    def _list_devices(self):
        """List the devices upsd knows about.

        Returns:
            list: Device names, empty when upsd cannot be reached
        """
        output = self._run(["-l", NUT_HOST])
        if output is None:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _read_variables(self, device):
        """Read all variables for a device.

        Args:
            device: Device name as listed by upsd

        Returns:
            dict or None: The variables, or None when the device has no status
        """
        output = self._run([f"{device}@{NUT_HOST}"])
        if output is None:
            return None
        variables = parse_variables(output)
        if STATUS_VARIABLE not in variables:
            return None
        return variables

    def _run(self, args):
        """Run upsc and return its output.

        Args:
            args: Arguments to pass to upsc

        Returns:
            str or None: Standard output, or None when the call failed
        """
        try:
            result = subprocess.run(
                [UPSC_BINARY, *args],
                capture_output=True,
                text=True,
                timeout=UPSC_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logging.debug(f"upsc {' '.join(args)} failed: {repr(e)}")
            return None
        if result.returncode != 0:
            logging.debug(f"upsc {' '.join(args)}: {result.stderr.strip()}")
            return None
        return result.stdout

    def _broadcast_if_changed(self):
        """Broadcast the status when it differs from the last one sent."""
        current = self.status_dict()
        if current == self._previous_status:
            return
        self._previous_status = current
        if self._socket_api is not None:
            self._socket_api.broadcast_status()
