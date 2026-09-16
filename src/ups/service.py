#!/usr/bin/env python3

"""UPS service main entry point.

Monitors power status via I2C and GPIO button input, provides status API
via Unix socket, and initiates shutdown when battery is critically low.
"""

import logging, platform, sys

from gpiozero import Button, LED
from gpiozero.exc import BadPinFactory, GPIOZeroError
from logging.handlers import SysLogHandler

from .unix_socket_api import UnixSocketApi

from .input_button import BlinkingButton
from .nut_monitor import NutMonitor
from .power_monitor import NO_UPS_ERROR, SystemPower, UpsNotDetectedError
from .settings import is_development

# GPIO pin number for power source button (GPIO 6 on Raspberry Pi)
POWER_SOURCE_BUTTON_PIN = 6

# GPIO pin number for UPS boot confirmation signal
# Must be held HIGH for entire service lifetime to signal UPS that Pi
# has booted successfully. Without this, UPS may not provide stable
# power output and may require double button press on next boot.
BOOT_CONFIRM_PIN = 12

# Logging interval for development mode (seconds)
DEV_LOG_INTERVAL = 1.5


def make_stdout_handler():
    """Create a stdout logging handler for development.
    
    Returns:
        logging.StreamHandler: Configured stdout handler
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s devel %(levelname)s [%(module)s] [PID %(process)d] %(message)s", "%b %d %H:%M:%S"
        )
    )
    return handler


def make_syslog_handler():
    """Create a syslog handler for production.
    
    Returns:
        logging.handlers.SysLogHandler: Configured syslog handler
    """
    handler = None
    if platform.system() == "Darwin":
        handler = SysLogHandler("/var/run/syslog")
    else:  # if platform.system() == 'Linux':
        handler = SysLogHandler("/dev/log")
    handler.setFormatter(logging.Formatter("%(levelname)s [%(module)s] [PID %(process)d] %(message)s"))
    return handler


def logging_setup():
    """Configure logging handlers and level based on environment.

    Debug messages are only emitted in development; production logs from info
    upwards, so expected conditions stay out of the journal.
    """
    logger = logging.getLogger()
    if not is_development():
        logger.addHandler(make_syslog_handler())
    else:
        logger.addHandler(make_stdout_handler())
    logger.setLevel(logging.DEBUG if is_development() else logging.INFO)


def boot_confirmation_signal():
    """Signal the UPS that the Pi has booted successfully.

    The pin must be held HIGH for the entire service lifetime. The UPS uses
    this signal to confirm a successful boot and maintain stable power output.
    Without it, the UPS may require a double button press on next boot or fail
    to start the Pi on first attempt.

    Returns:
        gpiozero.LED or None: The held pin, or None on a host without GPIO.
    """
    try:
        boot_pin = LED(BOOT_CONFIRM_PIN)
    except BadPinFactory:
        logging.debug(
            f"No GPIO on this host, skipping UPS boot confirmation signal "
            f"(GPIO {BOOT_CONFIRM_PIN})"
        )
        return None
    except (GPIOZeroError, OSError) as e:
        logging.warning(
            f"UPS boot confirmation signal unavailable (GPIO {BOOT_CONFIRM_PIN}): {repr(e)}"
        )
        return None
    boot_pin.on()
    logging.info(f"UPS boot confirmation signal set (GPIO {BOOT_CONFIRM_PIN} HIGH)")
    return boot_pin


def power_monitor():
    """Build the power monitor for this host.

    Returns:
        SystemPower or None: The monitor, or None when this host has no UPS to
        monitor because the battery gauge or the GPIO is missing.
    """
    try:
        return SystemPower(BlinkingButton(Button(POWER_SOURCE_BUTTON_PIN)))
    except BadPinFactory:
        logging.debug("No GPIO on this host, no UPS to monitor")
        return None
    except UpsNotDetectedError as e:
        logging.error(f"No UPS detected: {e}")
    except (GPIOZeroError, OSError) as e:
        logging.error(f"Cannot monitor a UPS on this host: {repr(e)}")
    return None


def serve_from_nut():
    """Serve the socket on a host with no GPIO UPS, reading NUT instead.

    Reports the no-UPS state until NUT answers for a device, so a node with no
    UPS at all answers on the socket exactly as before, while a USB UPS that is
    present or plugged in later is picked up and reported like the GPIO one.
    """
    logging.info(f"{NO_UPS_ERROR} on GPIO, reading NUT instead")
    monitor = NutMonitor()
    sock_handler = UnixSocketApi(monitor)
    monitor.set_socket_api(sock_handler)
    try:
        sock_handler.start()
        monitor.monitor_forever()
    except KeyboardInterrupt:
        print("\n[Ctrl-C] received, exiting...")
    finally:
        logging.info("Exiting")
        sock_handler.stop()
        monitor.stop()


def main():
    """Main service entry point.
    
    Initializes power monitoring, starts Unix socket API, and runs monitoring loop.
    Handles graceful shutdown on KeyboardInterrupt.
    """
    logging.info("Starting ups power management")

    boot_pin = boot_confirmation_signal()

    ups = power_monitor()
    if ups is None:
        if boot_pin is not None:
            boot_pin.off()
        serve_from_nut()
        return

    sock_handler = UnixSocketApi(ups)
    # Link socket API to power monitor for broadcasting changes
    ups.set_socket_api(sock_handler)
    try:
        sock_handler.start()
        if is_development():
            ups.log_forever(interval=DEV_LOG_INTERVAL)
        else:
            ups.monitor_forever()
    except KeyboardInterrupt:
        print("\n[Ctrl-C] received, exiting...")
    finally:
        logging.info("Exiting")
        sock_handler.stop()
        ups.stop()
        if boot_pin is not None:
            boot_pin.off()
            logging.info(f"UPS boot confirmation signal released (GPIO {BOOT_CONFIRM_PIN} LOW)")


if __name__ == "__main__":
    logging_setup()
    main()
