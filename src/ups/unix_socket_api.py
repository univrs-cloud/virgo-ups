import json
import logging
import os
import socket
import time
from threading import Thread, Lock

from .power_monitor import SystemPower

# Unix domain socket path for status API
UNIX_SOCKET_PATH = "/var/run/virgo-ups.sock"

# Socket permissions (readable/writable by all users)
SOCKET_MODE = 0o666

# Socket accept timeout (seconds)
SOCKET_TIMEOUT = 1.0


class UnixSocketApi:
    """Unix domain socket API for querying and monitoring UPS status.
    
    Maintains persistent connections with clients and broadcasts status updates
    when power source, charging state, capacity, or voltage changes.
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
        self._clients = []  # List of connected client sockets
        self._clients_lock = Lock()  # Lock for thread-safe client list access

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
        
        # Close all client connections
        with self._clients_lock:
            for client in self._clients[:]:
                try:
                    client.close()
                except Exception as e:
                    logging.warning(f"Error closing client connection: {e}")
            self._clients.clear()
        
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                logging.warning(f"Error closing socket: {e}")
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def broadcast_status(self):
        """Broadcast current status to all connected clients."""
        try:
            status = self._monitor.status_dict()
            message = f"{json.dumps(status)}\n".encode()
            
            with self._clients_lock:
                disconnected = []
                for client in self._clients:
                    try:
                        client.sendall(message)
                    except (OSError, BrokenPipeError, ConnectionResetError):
                        # Client disconnected
                        disconnected.append(client)
                    except Exception as e:
                        logging.warning(f"Error sending to client: {e}")
                        disconnected.append(client)
                
                # Remove disconnected clients
                for client in disconnected:
                    try:
                        client.close()
                    except Exception:
                        pass
                    self._clients.remove(client)
        except Exception as e:
            logging.error(f"Error broadcasting status: {e}")

    def _handle_client(self, conn):
        """Handle a persistent client connection.
        
        Args:
            conn: Client socket connection
        """
        try:
            # Set socket to non-blocking mode for keepalive checks
            conn.setblocking(False)
            
            # Send initial status immediately
            status = self._monitor.status_dict()
            initial_message = f"{json.dumps(status)}\n".encode()
            conn.sendall(initial_message)
            
            # Add client to list
            with self._clients_lock:
                self._clients.append(conn)
            
            # Keep connection alive - periodically check if client is still connected
            # Disconnection will be detected when we try to send broadcasts
            while self._running:
                try:
                    # Check if client is still connected by trying to peek.
                    # A 0-byte return means the peer closed the connection
                    # cleanly (EOF); an exception means it dropped.
                    peeked = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    if peeked == b"":
                        # Peer performed an orderly shutdown
                        break
                    # Client still connected (and sent data we ignore)
                    time.sleep(5)  # Check every 5 seconds
                except BlockingIOError:
                    # No data available, connection still alive
                    time.sleep(5)
                    continue
                except (OSError, BrokenPipeError, ConnectionResetError):
                    # Client disconnected
                    break
        except Exception as e:
            logging.warning(f"Error handling client: {e}")
        finally:
            # Remove client from list
            with self._clients_lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

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
            self._socket.listen(5)  # Allow multiple pending connections
            self._socket.settimeout(SOCKET_TIMEOUT)

            while self._running:
                try:
                    conn, _ = self._socket.accept()
                    # Handle each client in a separate thread for persistent connections
                    client_thread = Thread(target=self._handle_client, args=(conn,), daemon=True)
                    client_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:  # Only log if we're still supposed to be running
                        logging.error(f"Error accepting socket connection: {e}")
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
