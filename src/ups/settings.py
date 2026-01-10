"""Settings and configuration utilities."""

import logging, os


def is_development():
    """Check if running in development mode.
    
    Development mode is enabled when DEBUG environment variable is set
    and is not "no" or "false" (case-insensitive).
    
    Returns:
        bool: True if in development mode, False otherwise
    """
    debug = os.environ.get("DEBUG", None)
    return debug is not None and debug.lower() not in ["no", "false"]
