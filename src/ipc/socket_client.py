import json
import socket
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QThread, Signal


class SocketClient(QThread):
    """Qt thread for socket communication with the server."""

    message_received = Signal(dict)  # Now emits the parsed payload directly
    connection_status = Signal(bool)  # True for connected, False for disconnected

    def __init__(self, socket_path: str = "/tmp/axon-attendance.sock"):
        super().__init__()
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.message_handlers: list[Callable] = []

    def run(self):
        """Connect to the server socket and listen for messages."""
        self.running = True
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        try:
            self.sock.connect(self.socket_path)
            self.connection_status.emit(True)
            print(f"[SocketClient] Connected to server at {self.socket_path}")

            while self.running:
                try:
                    data = self.sock.recv(1024).decode()
                    if not data:
                        break

                    print(f"[SocketClient] Received from server: {data}")

                    # Parse JSON payload
                    try:
                        payload = json.loads(data)
                        print(f"[SocketClient] Parsed payload: {payload}")

                        # Emit the parsed payload
                        self.message_received.emit(payload)

                        # Process message through handlers
                        for handler in self.message_handlers:
                            try:
                                handler(payload)
                            except Exception as e:
                                print(f"[SocketClient] Handler error: {e}")

                    except json.JSONDecodeError as e:
                        print(f"[SocketClient] Invalid JSON received: {e}")
                        # Skip invalid JSON messages

                except Exception as e:
                    if self.running:
                        print(f"[SocketClient] Receive error: {e}")
                    break

        except Exception as e:
            print(f"[SocketClient] Connection error: {e}")
            self.connection_status.emit(False)
        finally:
            self.connection_status.emit(False)
            if self.sock:
                self.sock.close()
            self.running = False

    def send_message(self, payload: Dict[str, Any]) -> bool:
        """Send a JSON payload to the server. Returns True if successful.

        Args:
            payload: Dict to send (will be converted to JSON string)
        """
        if not self.sock or not self.running:
            return False

        try:
            if not isinstance(payload, dict):
                raise ValueError("Payload must be a dict")

            message = json.dumps(payload)
            self.sock.send(message.encode())
            print(f"[SocketClient] Sent to server: {message}")
            return True
        except Exception as e:
            print(f"[SocketClient] Send error: {e}")
            return False

    def stop(self):
        """Stop the socket client."""
        self.running = False
        if self.sock:
            self.sock.close()

    def add_message_handler(self, handler: Callable):
        """Add a message handler to process incoming messages from server.

        Args:
            handler: Function that receives (payload: dict) - the parsed JSON payload
        """
        self.message_handlers.append(handler)


class SocketManager:
    """Manager class for socket communication with the server."""

    def __init__(self, socket_path: str = "/tmp/axon-attendance.sock"):
        self.socket_path = socket_path
        self.client = SocketClient(socket_path)
        self.message_handlers: list[Callable] = []

    def start(self):
        """Start the socket client."""
        self.client.message_received.connect(self._handle_message)
        self.client.connection_status.connect(self._handle_connection_status)
        self.client.start()

    def stop(self):
        """Stop the socket client."""
        self.client.stop()
        self.client.wait()

    def send_message(self, payload: Dict[str, Any]) -> bool:
        """Send a JSON payload to the server.

        Args:
            payload: Dict to send (will be converted to JSON string)
        """
        return self.client.send_message(payload)

    def add_message_handler(self, handler: Callable):
        """Add a message handler.

        Args:
            handler: Function that receives (payload: dict) - the parsed JSON payload
        """
        self.message_handlers.append(handler)

    def _handle_message(self, payload: dict):
        """Handle incoming messages from the server."""
        for handler in self.message_handlers:
            try:
                handler(payload)
            except Exception as e:
                print(f"[SocketManager] Handler error: {e}")

    def _handle_connection_status(self, connected: bool):
        """Handle connection status changes."""
        status = "connected" if connected else "disconnected"
        print(f"[SocketManager] {status} to server")


# Global socket manager instance
_socket_manager: Optional[SocketManager] = None


def get_socket_manager() -> SocketManager:
    """Get the global socket manager instance, creating it if necessary."""
    global _socket_manager
    if _socket_manager is None:
        _socket_manager = SocketManager()
    return _socket_manager


def start_socket_client():
    """Start the global socket client."""
    manager = get_socket_manager()
    manager.start()


def stop_socket_client():
    """Stop the global socket client."""
    global _socket_manager
    if _socket_manager:
        _socket_manager.stop()
        _socket_manager = None


def send_message(payload: Dict[str, Any]) -> bool:
    """Send a JSON payload to the server.

    Args:
        payload: Dict to send (will be converted to JSON string)
    """
    manager = get_socket_manager()
    return manager.send_message(payload)


def add_message_handler(handler: Callable):
    """Add a message handler to process incoming messages from server.

    Args:
        handler: Function that receives (payload: dict) - the parsed JSON payload
    """
    manager = get_socket_manager()
    manager.add_message_handler(handler)
