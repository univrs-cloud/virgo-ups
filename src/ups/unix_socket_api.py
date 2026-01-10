import json
import logging
import os
import socket
from threading import Thread

from .power_monitor import SystemPower

# Unix domain socket path for status API
UNIX_SOCKET_PATH = "/var/run/virgo-ups.sock"

# Socket permissions (readable/writable by all users)
SOCKET_MODE = 0o666

# Socket accept timeout (seconds)
SOCKET_TIMEOUT = 1.0


class UnixSocketApi:
    """Unix domain socket API for querying UPS status.
    
    Provides a simple API endpoint that returns JSON status information
    when clients connect to the socket.
    """

    def __init__(self, monitor: SystemPower):
        """Initialize Unix socket API.
        
        Args:
            monitor: SystemPower instance to query for status
        """
        self._monitor = monitor
        self._running = False
        self._thread = None
        self._socket = None

    def start(self):
        """Start the Unix socket handler in a background thread."""
        if self._thread is not None:
            return

        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the Unix socket handler thread and clean up resources."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                logging.warning(f"Error closing socket: {e}")
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self):
        """Main socket handling loop running in background thread."""
        try:
            # Remove existing socket file if it exists
            if os.path.exists(UNIX_SOCKET_PATH):
                try:
                    os.unlink(UNIX_SOCKET_PATH)
                except Exception as e:
                    logging.warning(f"Error removing existing socket file: {e}")

            # Create and bind Unix domain socket
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.bind(UNIX_SOCKET_PATH)
            try:
                os.chmod(UNIX_SOCKET_PATH, SOCKET_MODE)
            except Exception as e:
                logging.warning(f"Error setting socket permissions: {e}")
            self._socket.listen(1)
            self._socket.settimeout(SOCKET_TIMEOUT)

            while self._running:
                try:
                    conn, _ = self._socket.accept()
                    try:
                        # Send JSON status and close immediately
                        data = self._monitor.status_dict()
                        data_bytes = f"{json.dumps(data)}\n".encode()
                        conn.sendall(data_bytes)
                    except Exception as e:
                        logging.error(f"Error sending status to client: {e}")
                    finally:
                        try:
                            conn.close()
                        except Exception as e:
                            logging.warning(f"Error closing client connection: {e}")
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:  # Only log if we're still supposed to be running
                        logging.error(f"Error handling socket connection: {e}")
                    continue

        except Exception as e:
            logging.error(f"Unix socket handler error: {e}")
        finally:
            # Clean up socket and socket file
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logging.warning(f"Error closing socket in cleanup: {e}")
            if os.path.exists(UNIX_SOCKET_PATH):
                try:
                    os.unlink(UNIX_SOCKET_PATH)
                except Exception as e:
                    logging.warning(f"Error removing socket file in cleanup: {e}")
