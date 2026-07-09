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
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.camera import open_camera
from src.config import APP_FRAME_HEIGHT, APP_FRAME_WIDTH, KCC_LOGO_PATH
from src.core.face_recognizer import FaceRecognizer
from src.ipc import (
    add_client_message_handler,
    send_message,
    start_socket_client,
    stop_socket_client,
)
from src.logger import get_core_ui_logger
from src.schema import (
    CadetAttendance,
    FaceIdentityMap,
    Person,
    Room,
    Session,
    db,
    ensure_db_schema,
)


class BasicApp(QMainWindow):
    # IPC messages arrive on the socket thread; these signals marshal the
    # camera/Qt work back onto the main thread (queued connection).
    enroll_prepare_requested = Signal(dict)
    enroll_capture_requested = Signal(dict)
    enroll_cancel_requested = Signal(dict)
    unenroll_requested = Signal(dict)
    delete_embedding_requested = Signal(dict)
    start_session_requested = Signal(dict)
    end_session_requested = Signal(dict)
    attendance_marked = Signal(dict)

    MODE_CAPTURE = "capture"
    MODE_ENROLLMENT = "enrollment"

    # Bottom-right indicator labels for each kiosk mode
    _MODE_DISPLAY = {
        MODE_CAPTURE: "Capture",
        MODE_ENROLLMENT: "Enroll",
    }

    def __init__(self):
        super().__init__()
        self.logger = get_core_ui_logger("ui")
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
        self.delete_embedding_requested.connect(self._handle_delete_embedding)
        self.start_session_requested.connect(self._handle_start_session)
        self.end_session_requested.connect(self._handle_end_session)
        self.attendance_marked.connect(self._handle_attendance_marked)
        self.init_ui()
        self.setup_socket_communication()
        self._enter_capture_mode()

    def init_ui(self):
        """Initialize the user interface.

        The central widget is a stack of two pages:

        Idle page — shown whenever there is no active session (and no
        enrollment in progress). Just the KCC logo with a prompt to start a
        new session; the camera is fully stopped so no faces are processed.

        Attendance page (fullscreen kiosk for a monitor):
            +-------------------+----------------------------------+
            |  Room Attendance  |   <session status>               |
            |  ┌─────────────┐  |   ┌────────────────────────────┐ |
            |  │ room table  │  |   │                            │ |
            |  │             │  |   │       camera (video)       │ |
            |  │             │  |   │                            │ |
            |  └─────────────┘  |   └────────────────────────────┘ |
            |                   |   ┌────────────────────────────┐ |
            |                   |   │  Admission No.  •  Name    │ |
            |                   |   └────────────────────────────┘ |
            +-------------------+----------------------------------+
        """
        self.setWindowTitle("Axon Attendance System")

        self.page_stack = QStackedWidget()
        self.setCentralWidget(self.page_stack)

        # ---- Idle page: KCC logo + "start a session" prompt ----
        self.idle_page = self._build_idle_page()
        self.page_stack.addWidget(self.idle_page)

        # ---- Attendance page: room table + camera + identity box ----
        self.attendance_page = QWidget()
        self.page_stack.addWidget(self.attendance_page)

        root = QHBoxLayout(self.attendance_page)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(20)

        # ---- Left panel: room attendance summary ----
        left = QVBoxLayout()
        left.setSpacing(10)

        self.room_table = QTableWidget(self)
        self.room_table.setColumnCount(4)
        self.room_table.setHorizontalHeaderLabels(
            ["Room name", "Total", "Present", "Absent"]
        )
        header = self.room_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.room_table.verticalHeader().setVisible(False)
        self.room_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.room_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.room_table.setAlternatingRowColors(True)
        left.addWidget(self.room_table, 1)
        self.room_table.setVisible(False)

        # Placeholder shown when no session is active so the left column
        # doesn't collapse to nothing.
        self.room_placeholder = QLabel("No active session")
        self.room_placeholder.setObjectName("placeholder")
        self.room_placeholder.setAlignment(Qt.AlignCenter)
        left.addWidget(self.room_placeholder, 1)

        # ---- Right panel: camera + identity info box ----
        right = QVBoxLayout()
        right.setSpacing(12)

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.setMinimumSize(640, 480)
        self.video_label.setStyleSheet(
            "background-color: #000000; border: 1px solid #dee2e6; border-radius: 10px;"
        )
        self.video_label.setVisible(False)
        right.addWidget(self.video_label, 1)

        # Identity box directly under the camera — shows admission no. + name
        # of the recognized person, or enrollment info when in enroll mode.
        # The widget stays in the layout (reserved space) but is visually
        # blanked via the `hidden` property when no face is recognized, so the
        # camera box above keeps a fixed height.
        self.info_label = QLabel("")
        self.info_label.setObjectName("infoBox")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setMinimumHeight(96)
        self.info_label.setProperty("hidden", True)
        right.addWidget(self.info_label)

        root.addLayout(left, 1)
        root.addLayout(right, 2)

        # Bottom-left session info (active session name + start timestamp, or
        # "No active session"). Added as a non-permanent status-bar widget so it
        # sits on the left, opposite the mode indicator.
        self.session_info_label = QLabel("No active session")
        self.session_info_label.setObjectName("sessionInfo")
        self.statusBar().addWidget(self.session_info_label, 1)

        # Bottom-right mode indicator (permanent status-bar widget → right-aligned)
        self.mode_indicator = QLabel(self._mode_display_text())
        self.mode_indicator.setObjectName("modeIndicator")
        self.mode_indicator.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.statusBar().addPermanentWidget(self.mode_indicator)

        # No session known at startup — begin on the idle page.
        self.page_stack.setCurrentWidget(self.idle_page)

        self.showFullScreen()
        self.apply_white_theme()

    def _build_idle_page(self) -> QWidget:
        """Idle screen shown when no session is active: logo + prompt only."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(28)

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap(str(KCC_LOGO_PATH))
        if pixmap.isNull():
            self.logger.warning("KCC logo not found at %s", KCC_LOGO_PATH)
            logo.setText("KCC")
            logo.setObjectName("idleLogoFallback")
        else:
            logo.setPixmap(pixmap.scaledToWidth(420, Qt.SmoothTransformation))
        layout.addWidget(logo)

        title = QLabel("No Active Session")
        title.setObjectName("idleTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Start a new session from the app to begin attendance.")
        subtitle.setObjectName("idleSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        return page

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
            self.logger.error("Camera open failed: %s", exc)
            self.info_label.setText(f"Camera error: {exc}")
            return

        # Initialize recognizer (reused across camera stop/start cycles —
        # InspireFace launch + FeatureHub enable must only happen once)
        if self.recognizer is None:
            self.recognizer = FaceRecognizer()
        # Pass current session id (if already resolved) so idempotency starts aligned
        try:
            self.recognizer.set_current_session(self.current_session_id)
            # Seed idempotency from DB for current session to avoid duplicates
            self._seed_recognizer_from_db_for_current_session()
        except Exception:
            self.logger.exception("Failed to seed recognizer for current session")

        # Timer to fetch frames and update UI ~30 FPS
        self.video_timer = QTimer(self)
        self.video_timer.timeout.connect(self._process_frame)
        self.video_timer.start(33)
        self.video_label.setVisible(True)

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
            self._refresh_enrollment_info(status)
            if status.done or status.failed:
                self._enrollment_finished(status)
        elif self.is_active_session and self.kiosk_mode == self.MODE_CAPTURE and self.recognizer:
            annotated, recognized = self.recognizer.recognize_faces(frame)
            self._refresh_recognition_info(recognized)
        else:
            annotated = frame

        # Convert to QImage and scale to fill the video area (keep aspect ratio)
        rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)
        scaled = pix.scaled(
            self.video_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.video_label.setPixmap(scaled)

    # --- Identity info box (below the camera) ----------------------------------

    def _set_session_info(self, active: bool, name: str | None = None,
                          start_timestamp: str | None = None) -> None:
        """Update the bottom status bar with the active session's name + start time."""
        if not hasattr(self, "session_info_label"):
            return
        if not active or not name:
            self.session_info_label.setText("No active session")
            return
        started = self._format_session_timestamp(start_timestamp)
        suffix = f"  ·  Started {started}" if started else ""
        self.session_info_label.setText(f"Session: {name}{suffix}")

    @staticmethod
    def _format_session_timestamp(raw: str | None) -> str:
        """Render an ISO session start timestamp as a readable local string."""
        if not raw:
            return ""
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(raw)
            return dt.strftime("%d %b %Y, %I:%M %p")
        except Exception:
            return raw

    def _refresh_recognition_info(self, recognized: list[dict]) -> None:
        """Update the info box with the recognized person's admission + name."""
        if self.kiosk_mode != self.MODE_CAPTURE:
            return
        if not self.is_active_session:
            self._set_info_box_hidden(True)
            self.info_label.setText("Waiting for session...")
            return
        if not recognized:
            self._set_info_box_hidden(True)
            self.info_label.setText("Waiting for face...")
            return
        # Show the first recognized person (kiosk is single-subject by design)
        person = recognized[0]
        name = person.get("name") or "Unknown"
        admission = person.get("admissionNumber") or "—"
        self.info_label.setText(
            f"<div style='font-size:16px;color:#cfe2ff;'>Admission No.</div>"
            f"<div style='font-size:34px;color:#ffffff;'>{admission}</div>"
            f"<div style='font-size:22px;color:#ffd43b;'>{name}</div>"
        )
        self._set_info_box_hidden(False)

    def _set_info_box_hidden(self, hidden: bool) -> None:
        """Toggle the info box between visually blank and shown.

        The widget always stays in the layout so the camera box above keeps a
        fixed height; when ``hidden`` is True the box is rendered transparent
        with no border and its text is cleared so nothing is visible.
        """
        self.info_label.setProperty("hidden", bool(hidden))
        if hidden:
            self.info_label.setText("")
        # Re-polish so the dynamic-property stylesheet selector applies.
        self.info_label.style().unpolish(self.info_label)
        self.info_label.style().polish(self.info_label)

    def _refresh_enrollment_info(self, status) -> None:
        """Show enrollment subject metadata + capture progress under the camera."""
        person = self.pending_enrollment or {}
        name = person.get("preferredName") or "—"
        admission = person.get("admissionNumber") or "—"
        room = person.get("roomId") or "—"
        bed = person.get("bedNumber")
        bed_text = str(bed) if bed is not None else "—"
        progress = ""
        if status and status.capturing:
            progress = f"<div style='font-size:18px;color:#ffd43b;'>Capturing {status.accepted}/{status.required}</div>"
        self.info_label.setText(
            f"<div style='font-size:14px;color:#cfe2ff;'>Enrolling — Admission No.</div>"
            f"<div style='font-size:34px;color:#ffffff;'>{admission}</div>"
            f"<div style='font-size:22px;color:#ffd43b;'>{name}</div>"
            f"<div style='font-size:15px;color:#d0ebff;'>Room {room} · Bed {bed_text}</div>"
            f"{progress}"
        )
        self._set_info_box_hidden(False)

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
                sess_name = None
                sess_start = None
                if is_active:
                    sess = data.get("session") or {}
                    sid = sess.get("id")
                    new_session_id = str(sid) if sid is not None else None
                    sess_name = sess.get("name")
                    sess_start = sess.get("startTimestamp")
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
                        self.logger.exception("recognizer session sync failed")
                self._set_session_info(is_active, sess_name, sess_start)
            else:
                self._set_session_info(False)
            self._set_active_ui(is_active)
        except Exception:
            self.logger.exception("query_current_session failed")
            self._set_session_info(False)
            self._set_active_ui(False)
            # When unreachable, consider session unknown
            self.current_session_id = None

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
        elif msg_type == "delete-embedding":
            self.delete_embedding_requested.emit(payload)
        elif msg_type == "start-session":
            self.start_session_requested.emit(payload)
        elif msg_type == "end-session":
            self.end_session_requested.emit(payload)
        elif msg_type == "enrollment-result":
            name = payload.get("name") or payload.get("personId")
            self.logger.info(
                "Enrollment %s: %s", payload.get("status"), name
            )
        elif msg_type == "attendance":
            self.attendance_marked.emit(payload)
        else:
            self.logger.debug("Unhandled server payload: %s", payload)

    # --- Kiosk modes ------------------------------------------------------------

    def _mode_display_text(self) -> str:
        """Human-readable label for the current kiosk mode."""
        return f"Mode: {self._MODE_DISPLAY.get(self.kiosk_mode, self.kiosk_mode)}"

    def _refresh_mode_indicator(self) -> None:
        """Sync the bottom-right mode indicator with the current kiosk mode."""
        if hasattr(self, "mode_indicator"):
            self.mode_indicator.setText(self._mode_display_text())

    def _enter_capture_mode(self) -> None:
        """Default mode: camera on and recognizing only while a session is active."""
        self.kiosk_mode = self.MODE_CAPTURE
        self.pending_enrollment = None
        if self.recognizer:
            self.recognizer.cancel_enrollment()
        self._update_view()
        self._refresh_recognition_info([])
        self._refresh_mode_indicator()

    def _update_view(self) -> None:
        """Pick the idle vs attendance page and gate the camera to match.

        The camera runs only when it is actually needed — during an active
        session or an admin-initiated enrollment. Otherwise it is fully
        released, so no faces can be detected, recognized, or saved while
        the kiosk is idle.
        """
        camera_needed = self.is_active_session or self.kiosk_mode == self.MODE_ENROLLMENT
        if camera_needed:
            self._ensure_camera_pipeline()
            self.video_label.setVisible(True)
            self.page_stack.setCurrentWidget(self.attendance_page)
        else:
            self._stop_camera_pipeline()
            self.page_stack.setCurrentWidget(self.idle_page)

    def _enter_enrollment_mode(self, payload: dict) -> None:
        """Show live preview and student metadata; wait for PWA capture."""
        person_id = payload.get("personId")
        name = payload.get("preferredName")
        user_type = payload.get("userType")
        if not person_id or not name or not user_type:
            self.logger.warning("Enrollment prepare missing fields: %s", payload)
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

        self._update_view()
        self.info_label.setText(
            f"<div style='font-size:14px;color:#cfe2ff;'>Enrolling — Admission No.</div>"
            f"<div style='font-size:34px;color:#ffffff;'>{payload.get('admissionNumber') or '—'}</div>"
            f"<div style='font-size:22px;color:#ffd43b;'>{name}</div>"
            f"<div style='font-size:15px;color:#d0ebff;'>Press Capture in the app when ready</div>"
        )
        self._set_info_box_hidden(False)
        self.logger.info("Enrollment prepared: %s", name)
        self._refresh_mode_indicator()

    def _on_enroll_prepare(self, payload: dict) -> None:
        self._enter_enrollment_mode(payload)

    def _on_enroll_capture(self, payload: dict) -> None:
        person_id = payload.get("personId")
        if not self.recognizer or not self.recognizer.is_enrolling:
            self.logger.warning(
                "Capture ignored — enrollment not prepared (%s)", person_id
            )
            return
        if (
            self.pending_enrollment
            and person_id
            and self.pending_enrollment.get("personId") != person_id
        ):
            self.logger.warning(
                "Capture personId mismatch: expected %s, got %s",
                self.pending_enrollment.get("personId"),
                person_id,
            )
            return
        try:
            self.recognizer.start_enrollment_capture()
            self.logger.info("Capture started")
        except Exception as exc:
            person = self.pending_enrollment or {}
            self._report_enrollment_failure(
                person.get("personId"),
                person.get("preferredName"),
                str(exc),
            )

    def _on_enroll_cancel(self, payload: dict) -> None:
        self.logger.info("Enrollment cancelled: %s", payload.get("personId"))
        self._enter_capture_mode()

    def _ensure_camera_pipeline(self) -> None:
        if self.cap is None or not getattr(self, "video_timer", None):
            self.setup_camera_pipeline()

    def _report_enrollment_failure(
        self, person_id: str | None, name: str | None, message: str
    ) -> None:
        self.logger.error("Enrollment failed: %s", message)
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
        self.logger.info("Enrollment: %s", status.message)
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
            if self.recognizer:
                self.recognizer.invalidate_identity(
                    hub_id=int(hub_id), person_id=payload.get("personId")
                )
            self.logger.info(
                "Removed enrollment for %s", payload.get("personId")
            )
        except Exception:
            self.logger.exception("Unenroll failed")

    def _handle_delete_embedding(self, payload: dict) -> None:
        """Remove embedding when cloud sends delete-embedding over IPC."""
        hub_id = payload.get("hubId")
        person_id = payload.get("personId") or payload.get("cadetId")
        if hub_id is not None:
            try:
                import inspireface as isf

                if self.recognizer is None:
                    self.recognizer = FaceRecognizer()
                isf.feature_hub_face_remove(int(hub_id))
                if self.recognizer:
                    self.recognizer.invalidate_identity(
                        hub_id=int(hub_id), person_id=person_id
                    )
                self.logger.info("Deleted embedding hubId=%s person=%s", hub_id, person_id)
            except Exception:
                self.logger.exception("delete-embedding failed for hubId=%s", hub_id)
        elif person_id and self.recognizer:
            try:
                if db.is_closed():
                    db.connect(reuse_if_open=True)
                mapping = FaceIdentityMap.get_or_none(
                    FaceIdentityMap.personId == person_id
                )
                if mapping:
                    import inspireface as isf

                    isf.feature_hub_face_remove(int(mapping.hubId))
                    if self.recognizer:
                        self.recognizer.invalidate_identity(
                            hub_id=int(mapping.hubId), person_id=person_id
                        )
                    self.logger.info("Deleted embedding for person=%s", person_id)
            except Exception:
                self.logger.exception("delete-embedding lookup failed")
            finally:
                try:
                    if not db.is_closed():
                        db.close()
                except Exception:
                    pass

    def _handle_start_session(self, payload: dict) -> None:
        """Apply start-session command from cloud sync."""
        session_id = payload.get("sessionId")
        if not session_id:
            return
        self.current_session_id = str(session_id)
        if self.recognizer:
            try:
                self.recognizer.set_current_session(self.current_session_id)
                self._seed_recognizer_from_db_for_current_session()
            except Exception:
                self.logger.exception("start-session recognizer sync failed")
        self._set_session_from_payload(payload, active=True)
        self.logger.info("Session started: %s", payload.get("name") or session_id)

    def _handle_end_session(self, payload: dict) -> None:
        """Apply end-session command from cloud sync."""
        session_id = payload.get("sessionId")
        if session_id and str(session_id) == str(self.current_session_id):
            self.current_session_id = None
            if self.recognizer:
                try:
                    self.recognizer.set_current_session(None)
                except Exception:
                    pass
        self._set_session_from_payload(payload, active=False)
        self.logger.info("Session ended: %s", session_id)

    def _handle_attendance_marked(self, payload: dict) -> None:
        """Refresh the room table when the API confirms a new attendance row."""
        name = payload.get("name") or payload.get("personId")
        self.logger.info("Attendance marked: %s", name)
        try:
            self.refresh_room_table()
        except Exception:
            self.logger.exception("refresh_room_table failed on attendance")

    def _set_session_from_payload(self, payload: dict, *, active: bool) -> None:
        name = payload.get("name")
        start_ts = payload.get("startTimestamp")
        self._set_session_info(active, name, start_ts)
        self._set_active_ui(active)
        if active:
            try:
                self.refresh_room_table()
            except Exception:
                self.logger.exception("refresh_room_table failed on start-session")

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
            # Keep self.recognizer alive: InspireFace launch + FeatureHub
            # enable are process-global and not safely repeatable. With the
            # camera released no frames reach it, so nothing is recognized.
            # Drop the last frame so a stale image doesn't flash when the
            # camera view comes back for the next session.
            self.video_label.clear()
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
            QLabel#sessionInfo {
                color: #495057;
                font-size: 15px;
                font-weight: 600;
                padding: 2px 8px;
            }
            QLabel#infoBox {
                background-color: #1864ab;
                color: #ffffff;
                font-size: 22px;
                font-weight: bold;
                border: 1px solid #0b4a8a;
                border-radius: 12px;
                padding: 16px 24px;
            }
            QLabel#infoBox[hidden="true"] {
                background-color: transparent;
                border: none;
                color: transparent;
            }
            QLabel#idleTitle {
                color: #212529;
                font-size: 36px;
                font-weight: 700;
            }
            QLabel#idleSubtitle {
                color: #868e96;
                font-size: 20px;
                font-weight: 500;
            }
            QLabel#idleLogoFallback {
                color: #1864ab;
                font-size: 72px;
                font-weight: 800;
            }
            QLabel#placeholder {
                color: #adb5bd;
                font-size: 18px;
                font-weight: 600;
                border: 1px dashed #dee2e6;
                border-radius: 10px;
            }
            QLabel#modeIndicator {
                background-color: #e7f5ff;
                color: #1864ab;
                font-size: 14px;
                font-weight: 600;
                padding: 4px 12px;
                border: 1px solid #1864ab;
                border-radius: 10px;
                margin: 2px 8px 2px 0px;
            }
            QTableWidget {
                background-color: #ffffff;
                border: 1px solid #dee2e6;
                border-radius: 8px;
                gridline-color: #e9ecef;
                font-size: 16px;
            }
            QTableView::item, QTableWidget::item {
                padding: 10px 8px;
            }
            QHeaderView::section {
                background-color: #f1f3f5;
                color: #495057;
                font-weight: 600;
                border: 1px solid #dee2e6;
                padding: 8px 8px;
                font-size: 15px;
            }
            QTableWidget::item:selected {
                background-color: #e7f5ff;
                color: #212529;
            }
        """)

    # --- Active session gating -------------------------------------------------
    def _set_active_ui(self, active: bool) -> None:
        """Toggle between the attendance page and the idle (logo) page."""
        if active == self.is_active_session:
            return
        self.is_active_session = active
        self._update_view()

        if active:
            self.room_table.setVisible(True)
            self.room_placeholder.setVisible(False)
            try:
                ensure_db_schema()
            except Exception:
                self.logger.exception("ensure_db_schema failed")
            self.refresh_room_table()
            if not hasattr(self, "room_table_timer") or self.room_table_timer is None:
                self.room_table_timer = QTimer(self)
                self.room_table_timer.timeout.connect(self.refresh_room_table)
            if not self.room_table_timer.isActive():
                self.room_table_timer.start(5000)
        else:
            self.room_table.setVisible(False)
            self.room_placeholder.setVisible(True)
            try:
                if hasattr(self, "room_table_timer") and self.room_table_timer:
                    self.room_table_timer.stop()
            except Exception:
                pass
        self._refresh_recognition_info([])

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
            self.logger.exception("Error loading room data: %s", exc)
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
