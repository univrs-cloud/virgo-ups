#!/usr/bin/env python3

"""UPS service main entry point.

Monitors power status via I2C and GPIO button input, provides status API
via Unix socket, and initiates shutdown when battery is critically low.
"""

import logging, platform, sys
import smbus

from datetime import datetime
from gpiozero import Button, LED
from logging.config import dictConfig
from logging.handlers import SysLogHandler
from threading import Thread, Lock

from .unix_socket_api import UnixSocketApi

from .input_button import BlinkingButton
from .power_monitor import SystemPower
from .settings import is_development

# GPIO pin number for power source button (GPIO 6 on Raspberry Pi)
POWER_SOURCE_BUTTON_PIN = 6

# GPIO pin number for X728 boot confirmation signal
# Must be held HIGH for entire service lifetime to signal X728 that Pi
# has booted successfully. Without this, X728 may not provide stable
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
    """Configure logging handlers based on environment."""
    logger = logging.getLogger()
    if not is_development():
        logger.addHandler(make_syslog_handler())
    else:
        logger.addHandler(make_stdout_handler())
    logger.setLevel(logging.DEBUG)


def main():
    """Main service entry point.
    
    Initializes power monitoring, starts Unix socket API, and runs monitoring loop.
    Handles graceful shutdown on KeyboardInterrupt.
    """
    logging.info("Starting ups power management")

    # Signal X728 that Pi has booted successfully.
    # This pin must be held HIGH for the entire service lifetime.
    # The X728 uses this signal to confirm successful boot and maintain
    # stable power output. Without it, the X728 may require a double
    # button press on next boot or fail to start the Pi on first attempt.
    boot_pin = LED(BOOT_CONFIRM_PIN)
    boot_pin.on()
    logging.info(f"X728 boot confirmation signal set (GPIO {BOOT_CONFIRM_PIN} HIGH)")

    ups = SystemPower(BlinkingButton(Button(POWER_SOURCE_BUTTON_PIN)))
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
        # Release boot confirmation pin on clean exit
        boot_pin.off()
        logging.info(f"X728 boot confirmation signal released (GPIO {BOOT_CONFIRM_PIN} LOW)")


if __name__ == "__main__":
    logging_setup()
    main()
