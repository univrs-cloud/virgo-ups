import json
import logging
import os
import socket
from threading import Thread

from .power_monitor import SystemPower


UNIX_SOCKET_PATH = "/var/run/virgo-ups.sock"


class UnixSocketApi:
    def __init__(self, monitor: SystemPower):
        self._monitor = monitor
        self._running = False
        self._thread = None
        self._socket = None

    def start(self):
        """Start the unix socket handler in a background thread"""
        if self._thread is not None:
            return

        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the unix socket handler thread"""
        self._running = False
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self):
        """Main socket handling loop running in background thread"""
        try:
            # Remove existing socket file if it exists
            if os.path.exists(UNIX_SOCKET_PATH):
                os.unlink(UNIX_SOCKET_PATH)

            # Create and bind unix domain socket
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.bind(UNIX_SOCKET_PATH)
            os.chmod(UNIX_SOCKET_PATH, 0o666)
            self._socket.listen(1)
            self._socket.settimeout(1.0)

            while self._running:
                try:
                    conn, _ = self._socket.accept()
                    try:
                        # Send JSON status and close immediately
                        data = self._monitor.status_dict()
                        data_bytes = f"{json.dumps(data)}\n".encode()
                        conn.sendall(data_bytes)
                    finally:
                        conn.close()
                except socket.timeout:
                    continue
                except Exception as e:
                    logging.error(f"Error handling socket connection: {e}")
                    continue

        except Exception as e:
            logging.error(f"Unix socket handler error: {e}")
        finally:
            if self._socket:
                self._socket.close()
            if os.path.exists(UNIX_SOCKET_PATH):
                os.unlink(UNIX_SOCKET_PATH)
