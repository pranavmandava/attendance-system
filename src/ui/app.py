"""
PySide6 Application that opens a camera, processes frames with FaceRecognizer,
and displays annotated video. IPC and session polling remain as-is.
"""

import sys

import cv2
from peewee import fn
from PySide6.QtCore import Qt, QTimer, Signal
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
    # IPC messages arrive on the socket thread; these signals marshal the
    # camera/Qt work back onto the main thread (queued connection).
    enroll_prepare_requested = Signal(dict)
    enroll_capture_requested = Signal(dict)
    enroll_cancel_requested = Signal(dict)
    unenroll_requested = Signal(dict)

    MODE_CAPTURE = "capture"
    MODE_ENROLLMENT = "enrollment"

    def __init__(self):
        super().__init__()
        self.cap = None
        self.recognizer = None
        self.is_active_session = False
        self.current_session_id = None
        self.kiosk_mode = self.MODE_CAPTURE
        self.pending_enrollment: dict | None = None
        self.enroll_prepare_requested.connect(self._on_enroll_prepare)
        self.enroll_capture_requested.connect(self._on_enroll_capture)
        self.enroll_cancel_requested.connect(self._on_enroll_cancel)
        self.unenroll_requested.connect(self._handle_unenroll)
        self.init_ui()
        self.setup_socket_communication()
        self._enter_capture_mode()

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

        self.status_label = QLabel("Capture mode — starting camera...")
        self.status_label.setAlignment(Qt.AlignLeft)
        left.addWidget(self.status_label)

        self.enrollment_label = QLabel("")
        self.enrollment_label.setAlignment(Qt.AlignLeft)
        self.enrollment_label.setWordWrap(True)
        self.enrollment_label.setVisible(False)
        left.addWidget(self.enrollment_label)

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
        # Initially hidden until camera pipeline is ready
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
        self.video_label.setVisible(True)
        if self.kiosk_mode == self.MODE_CAPTURE:
            self.status_label.setText("Capture mode — camera active")

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

        # Process frame: enrollment preview/capture takes priority over recognition
        if self.recognizer and self.recognizer.is_enrolling:
            annotated, status = self.recognizer.process_enrollment_frame(frame)
            if status.done or status.failed:
                self._enrollment_finished(status)
        elif self.is_active_session and self.kiosk_mode == self.MODE_CAPTURE and self.recognizer:
            annotated = self.recognizer.recognize_faces(frame)
        else:
            annotated = frame

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
        if msg_type == "enroll-prepare":
            self.enroll_prepare_requested.emit(payload)
        elif msg_type == "enroll-capture":
            self.enroll_capture_requested.emit(payload)
        elif msg_type == "enroll-cancel":
            self.enroll_cancel_requested.emit(payload)
        elif msg_type == "enroll-capture-request":
            # Legacy single-step clients: prepare then capture immediately
            self.enroll_prepare_requested.emit(payload)
            self.enroll_capture_requested.emit(payload)
        elif msg_type == "unenroll-request":
            self.unenroll_requested.emit(payload)
        elif msg_type == "enrollment-result":
            name = payload.get("name") or payload.get("personId")
            self.messages_display.append(
                f"Enrollment {payload.get('status')}: {name}"
            )
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

    # --- Kiosk modes ------------------------------------------------------------

    def _enter_capture_mode(self) -> None:
        """Default mode: camera on, recognition when a session is active."""
        self.kiosk_mode = self.MODE_CAPTURE
        self.pending_enrollment = None
        self.enrollment_label.setVisible(False)
        if self.recognizer:
            self.recognizer.cancel_enrollment()
        self._ensure_camera_pipeline()
        self.video_label.setVisible(True)
        if self.is_active_session:
            self.status_label.setText("Capture mode — attendance session active")
        else:
            self.status_label.setText("Capture mode — waiting for session")

    def _enter_enrollment_mode(self, payload: dict) -> None:
        """Show live preview and student metadata; wait for PWA capture."""
        person_id = payload.get("personId")
        name = payload.get("preferredName")
        user_type = payload.get("userType")
        if not person_id or not name or not user_type:
            self.messages_display.append(
                f"Enrollment prepare missing fields: {payload}"
            )
            return

        self._ensure_camera_pipeline()
        if not self.recognizer:
            self._report_enrollment_failure(
                person_id, name, "Camera unavailable"
            )
            return

        self.kiosk_mode = self.MODE_ENROLLMENT
        self.pending_enrollment = payload
        self.recognizer.arm_enrollment(payload)

        admission = payload.get("admissionNumber") or "—"
        room = payload.get("roomId") or "—"
        bed = payload.get("bedNumber")
        bed_text = str(bed) if bed is not None else "—"

        self.enrollment_label.setText(
            f"Enrollment mode\n"
            f"Name: {name}\n"
            f"Admission: {admission}\n"
            f"Room: {room}\n"
            f"Bed: {bed_text}\n"
            f"Press Capture in the app when ready"
        )
        self.enrollment_label.setVisible(True)
        self.video_label.setVisible(True)
        self.status_label.setText("Enrollment mode — waiting for capture")
        self.messages_display.append(f"Enrollment prepared: {name}")

    def _on_enroll_prepare(self, payload: dict) -> None:
        self._enter_enrollment_mode(payload)

    def _on_enroll_capture(self, payload: dict) -> None:
        person_id = payload.get("personId")
        if not self.recognizer or not self.recognizer.is_enrolling:
            self.messages_display.append(
                f"Capture ignored — enrollment not prepared ({person_id})"
            )
            return
        if (
            self.pending_enrollment
            and person_id
            and self.pending_enrollment.get("personId") != person_id
        ):
            self.messages_display.append(
                f"Capture personId mismatch: expected "
                f"{self.pending_enrollment.get('personId')}, got {person_id}"
            )
            return
        try:
            self.recognizer.start_enrollment_capture()
            self.status_label.setText("Enrollment mode — capturing face")
            self.messages_display.append("Capture started")
        except Exception as exc:
            person = self.pending_enrollment or {}
            self._report_enrollment_failure(
                person.get("personId"),
                person.get("preferredName"),
                str(exc),
            )

    def _on_enroll_cancel(self, payload: dict) -> None:
        self.messages_display.append(
            f"Enrollment cancelled: {payload.get('personId')}"
        )
        self._enter_capture_mode()

    def _ensure_camera_pipeline(self) -> None:
        if self.cap is None or not getattr(self, "video_timer", None):
            self.setup_camera_pipeline()

    def _report_enrollment_failure(
        self, person_id: str | None, name: str | None, message: str
    ) -> None:
        self.messages_display.append(f"Enrollment failed: {message}")
        send_message(
            {
                "type": "enrollment-result",
                "status": "failed",
                "personId": person_id,
                "name": name,
                "message": message,
            }
        )
        self._enter_capture_mode()

    def _enrollment_finished(self, status) -> None:
        """Report capture result over IPC and return to capture mode."""
        person = status.person or self.pending_enrollment or {}
        self.messages_display.append(f"Enrollment: {status.message}")
        send_message(
            {
                "type": "enrollment-result",
                "status": "completed" if status.done else "failed",
                "personId": person.get("personId"),
                "name": person.get("preferredName"),
                "message": status.message,
            }
        )
        self._enter_capture_mode()

    def _handle_unenroll(self, payload: dict) -> None:
        """Remove an embedding from FeatureHub on behalf of the API process."""
        hub_id = payload.get("hubId")
        if hub_id is None:
            return
        try:
            import inspireface as isf

            # FeatureHub must be enabled (recognizer alive) to remove features
            if self.recognizer is None:
                self.recognizer = FaceRecognizer()
            isf.feature_hub_face_remove(int(hub_id))
            self.messages_display.append(
                f"Removed enrollment for {payload.get('personId')}"
            )
        except Exception as exc:
            self.messages_display.append(f"Unenroll failed: {exc}")

    def _stop_camera_pipeline(self) -> None:
        """Stop the frame timer and release the camera/recognizer."""
        try:
            if hasattr(self, "video_timer") and self.video_timer:
                self.video_timer.stop()
                self.video_timer = None
            if self.cap:
                try:
                    self.cap.release()
                except Exception:
                    pass
            self.cap = None
            self.recognizer = None
        except Exception:
            pass

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
        """Toggle attendance session UI; camera stays on in capture mode."""
        if active == self.is_active_session:
            return
        self.is_active_session = active

        if active:
            self.room_table.setVisible(True)
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
            if self.kiosk_mode == self.MODE_CAPTURE:
                self.status_label.setText("Capture mode — attendance session active")
        else:
            self.room_table.setVisible(False)
            try:
                if hasattr(self, "room_table_timer") and self.room_table_timer:
                    self.room_table_timer.stop()
            except Exception:
                pass
            if self.kiosk_mode == self.MODE_CAPTURE:
                self.status_label.setText("Capture mode — waiting for session")

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
