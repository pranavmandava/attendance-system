import json
import os
import queue
import socket
import threading
from typing import Any, Callable, Dict, List, Optional


class SocketServer:
    """Unix socket server for IPC communication with UI clients."""

    def __init__(self, socket_path: str = "/tmp/axon-attendance.sock"):
        self.socket_path = socket_path
        self.server_socket: Optional[socket.socket] = None
        self.clients: List["Client"] = []
        self.clients_lock = threading.Lock()
        self.message_handlers: List[Callable] = []
        self.running = False

        # Remove existing socket file if present
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def start(self):
        """Start the socket server in a background thread."""
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.socket_path)
        self.server_socket.listen()
        self.running = True

        # Start socket listener in a separate thread
        threading.Thread(target=self._socket_listener, daemon=True).start()
        print(f"[SocketServer] Started listening on {self.socket_path}")

    def stop(self):
        """Stop the socket server and cleanup."""
        self.running = False
        if self.server_socket:
            self.server_socket.close()

        # Close all client connections
        with self.clients_lock:
            for client in self.clients:
                client.stop()
            self.clients.clear()

        # Remove socket file
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def broadcast(self, payload: Dict[str, Any]) -> int:
        """Broadcast a JSON payload to all connected UI clients.

        Args:
            payload: Dict to send (will be converted to JSON string)

        Returns:
            Number of clients the message was queued for.
        """
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a dict")

        message = json.dumps(payload)
        with self.clients_lock:
            for client in self.clients:
                client.send_message(message)
            return len(self.clients)

    def add_message_handler(self, handler: Callable):
        """Add a message handler to process incoming messages from UI.

        Args:
            handler: Function that receives (payload: dict) - the parsed JSON payload
        """
        self.message_handlers.append(handler)

    def _socket_listener(self):
        """Accept new socket connections from UI clients."""
        while self.running:
            try:
                conn, _ = self.server_socket.accept()
                client = Client(conn, self.message_handlers)
                with self.clients_lock:
                    self.clients.append(client)
                print(
                    f"[SocketServer] New client connected. Total clients: {len(self.clients)}"
                )
            except Exception as e:
                if self.running:
                    print(f"[SocketServer] Accept error: {e}")
                break


class Client:
    """Represents a connected UI client."""

    def __init__(self, conn: socket.socket, message_handlers: List[Callable]):
        self.conn = conn
        self.send_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.message_handlers = message_handlers

        # Start receive and send threads
        self.recv_thread = threading.Thread(target=self._recv_loop)
        self.send_thread = threading.Thread(target=self._send_loop)
        self.recv_thread.start()
        self.send_thread.start()

    def send_message(self, message: str):
        """Queue a message to be sent to this client."""
        if not self.stop_event.is_set():
            self.send_queue.put(message)

    def stop(self):
        """Stop this client and cleanup."""
        self.stop_event.set()
        self.conn.close()

    def _recv_loop(self):
        """Handle messages received from the UI via socket."""
        try:
            while not self.stop_event.is_set():
                data = self.conn.recv(1024).decode()
                if not data:
                    break

                print(f"[SocketServer] Received from UI: {data}")

                # Parse JSON payload
                try:
                    payload = json.loads(data)
                    print(f"[SocketServer] Parsed payload: {payload}")

                    # Process message through all handlers
                    for handler in self.message_handlers:
                        try:
                            handler(payload)
                        except Exception as e:
                            print(f"[SocketServer] Handler error: {e}")

                except json.JSONDecodeError as e:
                    print(f"[SocketServer] Invalid JSON received: {e}")
                    # Skip invalid JSON messages

        except Exception as e:
            print(f"[SocketServer] Receive error: {e}")
        finally:
            self.stop_event.set()
            self.conn.close()

    def _send_loop(self):
        """Send messages to the UI from the send queue."""
        try:
            while not self.stop_event.is_set():
                try:
                    message = self.send_queue.get(timeout=1)
                    if message is None:
                        break
                    self.conn.send(message.encode())
                except queue.Empty:
                    continue
        except Exception as e:
            print(f"[SocketServer] Send error: {e}")
        finally:
            self.stop_event.set()
            self.conn.close()


# Global socket server instance
_socket_server: Optional[SocketServer] = None


def get_socket_server() -> SocketServer:
    """Get the global socket server instance, creating it if necessary."""
    global _socket_server
    if _socket_server is None:
        _socket_server = SocketServer()
    return _socket_server


def start_socket_server():
    """Start the global socket server."""
    server = get_socket_server()
    server.start()


def stop_socket_server():
    """Stop the global socket server."""
    global _socket_server
    if _socket_server:
        _socket_server.stop()
        _socket_server = None


def broadcast_message(payload: Dict[str, Any]) -> int:
    """Broadcast a JSON payload to all connected UI clients.

    Args:
        payload: Dict to send (will be converted to JSON string)

    Returns:
        Number of clients the message was queued for.
    """
    server = get_socket_server()
    return server.broadcast(payload)


def add_message_handler(handler: Callable):
    """Add a message handler to process incoming messages from UI.

    Args:
        handler: Function that receives (payload: dict) - the parsed JSON payload
    """
    server = get_socket_server()
    server.add_message_handler(handler)
