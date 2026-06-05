"""
PySide6 Application that opens a camera, processes frames with FaceRecognizer,
and displays annotated video. IPC and session polling remain as-is.
"""

import sys

import cv2
from peewee import fn
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.camera import open_camera
from src.config import APP_FRAME_HEIGHT, APP_FRAME_WIDTH
from src.core.face_recognizer import FaceRecognizer
from src.ipc import (
    add_client_message_handler,
    send_message,
    start_socket_client,
    stop_socket_client,
)
from src.schema import CadetAttendance, Person, Room, Session, db, ensure_db_schema


class BasicApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cap = None
        self.recognizer = None
        self.is_active_session = False
        self.current_session_id = None  # Track active session id for idempotency
        self.init_ui()
        self.setup_socket_communication()

    def init_ui(self):
        """Initialize the user interface"""
        # Set window title
        self.setWindowTitle("Axon Attendance System")

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Root split: left controls, right video
        root = QHBoxLayout(central_widget)
        root.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # Left side (controls)
        left = QVBoxLayout()
        left.setAlignment(Qt.AlignTop)

        self.status_label = QLabel("Connecting to server...")
        self.status_label.setAlignment(Qt.AlignLeft)
        left.addWidget(self.status_label)

        self.messages_display = QTextEdit()
        self.messages_display.setReadOnly(True)
        self.messages_display.setMinimumWidth(420)
        self.messages_display.setMinimumHeight(220)
        left.addWidget(self.messages_display)

        # Room attendance table
        self.room_table = QTableWidget(self)
        self.room_table.setColumnCount(4)
        self.room_table.setHorizontalHeaderLabels(
            ["Room name", "Total count", "Present", "Absent"]
        )
        header = self.room_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.room_table.verticalHeader().setVisible(False)
        self.room_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.room_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.room_table.setAlternatingRowColors(True)
        left.addWidget(self.room_table)

        # Right side (video)
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setFixedSize(APP_FRAME_WIDTH, APP_FRAME_HEIGHT)
        self.video_label.setStyleSheet(
            "background-color: #000000; border: 1px solid #dee2e6;"
        )
        # Initially hidden until an active session is detected
        self.video_label.setVisible(False)
        self.room_table.setVisible(False)

        # Add to root with order: left then right (video at rightmost)
        root.addLayout(left, 0)
        root.addWidget(self.video_label, 1)

        # Set window size
        self.showFullScreen()

        # Apply white theme using stylesheet only
        self.apply_white_theme()

    def setup_socket_communication(self):
        """Setup socket communication with the server."""
        # Start socket client
        start_socket_client()

        # Add message handler
        add_client_message_handler(self.handle_server_message)

        # Update status based on current session state
        self.query_current_session()

        # Periodically refresh session state as a simple placeholder
        self.session_timer = QTimer(self)
        self.session_timer.timeout.connect(self.query_current_session)
        self.session_timer.start(5000)

    def setup_camera_pipeline(self):
        """Open camera via OpenCV and start processing frames with the recognizer."""
        try:
            self.cap = open_camera()
        except RuntimeError as exc:
            self.messages_display.append(f"Error: {exc}")
            return

        # Initialize recognizer
        self.recognizer = FaceRecognizer()
        # Pass current session id (if already resolved) so idempotency starts aligned
        try:
            self.recognizer.set_current_session(self.current_session_id)
            # Seed idempotency from DB for current session to avoid duplicates
            self._seed_recognizer_from_db_for_current_session()
        except Exception:
            pass

        # Timer to fetch frames and update UI ~30 FPS
        self.video_timer = QTimer(self)
        self.video_timer.timeout.connect(self._process_frame)
        self.video_timer.start(33)

    def _process_frame(self):
        if not self.cap or not self.recognizer:
            return
        ret, frame = self.cap.read()
        if not ret:
            return
        # Ensure configured resolution for InspireFace processing and display
        if frame.shape[0] != APP_FRAME_HEIGHT or frame.shape[1] != APP_FRAME_WIDTH:
            frame = cv2.resize(
                frame,
                (APP_FRAME_WIDTH, APP_FRAME_HEIGHT),
                interpolation=cv2.INTER_LINEAR,
            )

        # Process frame
        annotated = self.recognizer.recognize_faces(frame)

        # Convert to QImage (no zoom: scale with aspect ratio to label size)
        rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)
        # Display at native configured resolution without extra scaling
        self.video_label.setPixmap(pix)

    def query_current_session(self):
        """Fetch current session status from Flask API and update label."""
        try:
            import requests

            resp = requests.get("http://127.0.0.1:1337/session/current", timeout=2)
            is_active = False
            if resp.status_code == 200:
                data = resp.json()
                is_active = bool(data.get("active") and data.get("session"))
                # Capture session id (stringify for consistency across DB/HTTP)
                new_session_id = None
                old_session_id = getattr(self, "current_session_id", None)
                if is_active:
                    sess = data.get("session") or {}
                    sid = sess.get("id")
                    new_session_id = str(sid) if sid is not None else None
                self.current_session_id = new_session_id
                # Inform recognizer so per-session idempotency set is kept correct
                if self.recognizer:
                    try:
                        self.recognizer.set_current_session(self.current_session_id)
                        # On session change, seed recognizer with already-present IDs
                        if new_session_id and new_session_id != old_session_id:
                            self._seed_recognizer_from_db_for_current_session()
                            # Immediately refresh table to reflect new session
                            try:
                                if self.is_active_session:
                                    self.refresh_room_table()
                            except Exception:
                                pass
                    except Exception:
                        pass
                if is_active:
                    self.status_label.setText(
                        f"Attendance system is active: {data.get('session').get('name')}"
                    )
                else:
                    self.status_label.setText("Please create a new session")
            else:
                self.status_label.setText("Please create a new session")
            self._set_active_ui(is_active)
        except Exception:
            self.status_label.setText("Please create a new session")
            self._set_active_ui(False)
            # When unreachable, consider session unknown
            self.current_session_id = None

    def send_message(self):
        """Send a message to the server."""
        message = self.message_input.text().strip()
        if message:
            # Send via socket as a dictionary payload
            payload = {
                "type": "user_message",
                "message": message,
                "timestamp": None,  # Could add timestamp if needed
            }
            success = send_message(payload)
            if success:
                self.messages_display.append(f"You: {message}")
                self.message_input.clear()
            else:
                self.messages_display.append("Error: Failed to send message")

    def handle_server_message(self, payload: dict):
        """Handle messages received from the server."""
        msg_type = payload.get("type")
        if msg_type == "enrollment":
            name = payload.get("name") or payload.get("personId")
            self.messages_display.append(f"Enrollment completed: {name}")
        elif msg_type == "attendance":
            name = payload.get("name") or payload.get("personId")
            self.messages_display.append(f"Attendance marked: {name}")
            # Refresh the room table to reflect new present counts
            try:
                self.refresh_room_table()
            except Exception:
                pass
        else:
            # Fallback display for any other payloads
            self.messages_display.append(f"Server: {payload}")

    def closeEvent(self, event):
        """Handle application close event."""
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        if hasattr(self, "video_timer") and self.video_timer:
            self.video_timer.stop()
        if hasattr(self, "room_table_timer") and self.room_table_timer:
            self.room_table_timer.stop()
        stop_socket_client()
        event.accept()

    def apply_white_theme(self):
        """Apply white theme to the application using stylesheet only"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #ffffff;
            }
            QWidget {
                background-color: #ffffff;
                color: #323232;
            }
            QLabel {
                background-color: transparent;
                color: #323232;
                font-size: 18px;
                font-weight: bold;
            }
            QTextEdit {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
                font-family: monospace;
                font-size: 12px;
            }
            QLineEdit {
                background-color: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
                font-size: 14px;
            }
            QTableWidget {
                background-color: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                gridline-color: #e9ecef;
                font-size: 14px;
            }
            QTableView::item, QTableWidget::item {
                padding: 6px;
            }
            QHeaderView::section {
                background-color: #f1f3f5;
                color: #495057;
                font-weight: 600;
                border: 1px solid #dee2e6;
                padding: 6px 8px;
            }
            QTableWidget::item:selected {
                background-color: #e7f5ff;
                color: #212529;
            }
        """)

    # --- Active session gating -------------------------------------------------
    def _set_active_ui(self, active: bool) -> None:
        """Show/hide InspireFace video and room table based on session state.

        Also starts/stops camera pipeline and table refresh timer accordingly.
        """
        if active == self.is_active_session:
            return
        self.is_active_session = active

        if active:
            # Show widgets
            self.video_label.setVisible(True)
            self.room_table.setVisible(True)

            # Start camera/recognizer if not started
            if self.cap is None or not getattr(self, "video_timer", None):
                self.setup_camera_pipeline()

            # Ensure table/timer
            try:
                ensure_db_schema()
            except Exception:
                pass
            self.refresh_room_table()
            if not hasattr(self, "room_table_timer") or self.room_table_timer is None:
                self.room_table_timer = QTimer(self)
                self.room_table_timer.timeout.connect(self.refresh_room_table)
            if not self.room_table_timer.isActive():
                self.room_table_timer.start(5000)
        else:
            # Hide widgets
            self.video_label.setVisible(False)
            self.room_table.setVisible(False)

            # Stop camera/recognizer and timer
            try:
                if hasattr(self, "video_timer") and self.video_timer:
                    self.video_timer.stop()
                if self.cap:
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                self.cap = None
                self.recognizer = None
            except Exception:
                pass

            try:
                if hasattr(self, "room_table_timer") and self.room_table_timer:
                    self.room_table_timer.stop()
            except Exception:
                pass

        # Reset optimistic state on session toggle
        # No optimistic UI overlay; counts come solely from DB

    def refresh_room_table(self) -> None:
        """Populate the room attendance table from the database."""
        try:
            if db.is_closed():
                db.connect(reuse_if_open=True)

            # Determine active session (prefer UI-known id, fallback to DB)
            session_id_to_use = getattr(self, "current_session_id", None)
            if not session_id_to_use:
                active_session = Session.get_or_none(
                    Session.actualEndTimestamp.is_null()
                )
                session_id_to_use = active_session.id if active_session else None

            # Precompute totals per room from Person
            totals_by_room: dict[str, int] = {}
            totals_query = (
                Person.select(Person.roomId, fn.COUNT(Person.uniqueId).alias("cnt"))
                .where((Person.roomId.is_null(False)) & (Person.roomId != ""))
                .group_by(Person.roomId)
            )
            for row in totals_query:
                totals_by_room[row.roomId] = int(row.cnt)

            # Precompute present per room for active session using DISTINCT personId
            present_by_room: dict[str, int] = {}
            if session_id_to_use is not None:
                present_rows = (
                    CadetAttendance.select(
                        Person.roomId,
                        fn.COUNT(fn.DISTINCT(CadetAttendance.personId)).alias("cnt"),
                    )
                    .join(Person, on=(Person.uniqueId == CadetAttendance.personId))
                    .where(
                        (CadetAttendance.sessionId == session_id_to_use)
                        & (Person.roomId.is_null(False))
                        & (Person.roomId != "")
                    )
                    .group_by(Person.roomId)
                    .tuples()
                )
                for room_id, cnt in present_rows:
                    present_by_room[str(room_id)] = int(cnt)

            # Fetch rooms to display
            rooms = list(Room.select().order_by(Room.roomName.asc()))
            self.room_table.setRowCount(len(rooms))

            for idx, room in enumerate(rooms):
                room_name = room.roomName or room.roomId
                total = int(totals_by_room.get(room.roomId, 0))
                present = int(present_by_room.get(room.roomId, 0))
                absent = max(total - present, 0)

                # Room name
                item_room = QTableWidgetItem(str(room_name))
                item_room.setFlags(item_room.flags() & ~Qt.ItemIsEditable)
                self.room_table.setItem(idx, 0, item_room)

                # Total
                item_total = QTableWidgetItem(str(total))
                item_total.setTextAlignment(Qt.AlignCenter)
                item_total.setFlags(item_total.flags() & ~Qt.ItemIsEditable)
                self.room_table.setItem(idx, 1, item_total)

                # Present
                item_present = QTableWidgetItem(str(present))
                item_present.setTextAlignment(Qt.AlignCenter)
                item_present.setFlags(item_present.flags() & ~Qt.ItemIsEditable)
                self.room_table.setItem(idx, 2, item_present)

                # Absent
                item_absent = QTableWidgetItem(str(absent))
                item_absent.setTextAlignment(Qt.AlignCenter)
                item_absent.setFlags(item_absent.flags() & ~Qt.ItemIsEditable)
                self.room_table.setItem(idx, 3, item_absent)

        except Exception as exc:
            # Basic fallback: show the error in the messages display
            try:
                self.messages_display.append(f"Error loading room data: {exc}")
            except Exception:
                pass
        finally:
            try:
                if not db.is_closed():
                    db.close()
            except Exception:
                pass

    # --- Idempotency seeding ---------------------------------------------------
    def _seed_recognizer_from_db_for_current_session(self) -> None:
        """Seed recognizer idempotency from DB for the active session."""
        try:
            if not self.recognizer or not self.current_session_id:
                return
            if db.is_closed():
                db.connect(reuse_if_open=True)
            rows = (
                CadetAttendance.select(CadetAttendance.personId)
                .where(CadetAttendance.sessionId == self.current_session_id)
                .distinct()
                .tuples()
            )
            person_ids = [pid for (pid,) in rows if pid]
            if person_ids:
                self.recognizer.seed_current_session(person_ids)
        except Exception:
            pass
        finally:
            try:
                if not db.is_closed():
                    db.close()
            except Exception:
                pass


def main():
    """Main function to run the application"""
    app = QApplication(sys.argv)

    # Create and show the main window
    BasicApp()

    # Start the event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
