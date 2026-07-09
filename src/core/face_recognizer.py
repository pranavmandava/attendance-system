import platform
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

import cv2
import inspireface as isf

from src.config import INSPIREFACE_MODEL_NAME
from src.core.enrollment import CaptureStatus, EnrollmentCapture, enroll_from_image
from src.core.inspireface_engine import create_session, model_dir
from src.ipc import send_message
from src.logger import get_core_ui_logger
from src.schema import FaceIdentityMap, Person, db
from src.utils import ist_timestamp


class FaceRecognizer:
    """Face recognition and enrollment using InspireFace FeatureHub."""

    def __init__(self):
        self.logger = get_core_ui_logger("face_recognizer")
        self.current_session_id: Optional[str] = None
        self._attendance_marked_by_session: Dict[str, Set[str]] = {}
        self.on_first_attendance: Optional[Callable[[str], None]] = None
        self._enrollment: Optional[EnrollmentCapture] = None
        # hubId -> {"personId": str, "name": str, "admissionNumber": str} | None
        # None means "known-absent" (searched, no mapping) to avoid re-querying misses.
        self._identity_cache: Dict[int, Optional[dict]] = {}

        try:
            self.logger.info("FaceRecognizer initialisation started")
            self.logger.debug(
                "Platform detected: system=%s, machine=%s",
                platform.system(),
                platform.machine(),
            )

            self.is_rockchip = platform.system() == "Linux" and (
                platform.machine() in {"aarch64", "arm64"}
            )
            self.logger.info(
                "Using model '%s' at %s (rockchip=%s)",
                INSPIREFACE_MODEL_NAME,
                model_dir(),
                self.is_rockchip,
            )

            self.session = create_session()
            print("InspireFace session created and FeatureHub enabled.")
        except Exception as e:
            self.logger.exception("Exception during FaceRecognizer initialisation")
            print(f"Error creating InspireFace session: {e}")
            raise SystemExit(1) from e

    def set_current_session(self, session_id: Optional[str]) -> None:
        if session_id == self.current_session_id:
            return

        self.current_session_id = session_id
        if session_id is None:
            return

        if session_id not in self._attendance_marked_by_session:
            self._attendance_marked_by_session[session_id] = set()

    def add_attendance_if_new(self, person_id: str) -> bool:
        if not self.current_session_id:
            return False
        seen_set = self._attendance_marked_by_session.setdefault(
            self.current_session_id, set()
        )
        if person_id in seen_set:
            return False
        seen_set.add(person_id)
        return True

    def get_attendance_marked_tuple(self) -> Tuple[str, ...]:
        if not self.current_session_id:
            return tuple()
        return tuple(
            self._attendance_marked_by_session.get(self.current_session_id, set())
        )

    def seed_attendance_for_session(
        self, session_id: Optional[str], person_ids: Iterable[str]
    ) -> None:
        if not session_id or not person_ids:
            return
        session_set = self._attendance_marked_by_session.setdefault(session_id, set())
        for pid in person_ids:
            if pid:
                session_set.add(pid)

    def seed_current_session(self, person_ids: Iterable[str]) -> None:
        self.seed_attendance_for_session(self.current_session_id, person_ids)

    def set_on_first_attendance_callback(
        self, callback: Optional[Callable[[str], None]]
    ) -> None:
        self.on_first_attendance = callback

    def resolve_identity(self, hub_id: int) -> Optional[dict]:
        """Return identity dict for a FeatureHub id, using an in-memory cache.

        On a miss, open the DB once, look up FaceIdentityMap -> Person, and cache
        the result (including a cached None for 'no mapping') so the recognition
        loop does not touch SQLite per frame.
        """
        if hub_id in self._identity_cache:
            return self._identity_cache[hub_id]
        identity = None
        try:
            if db.is_closed():
                db.connect(reuse_if_open=True)
            mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.hubId == hub_id)
            if mapping:
                person = Person.get_or_none(Person.uniqueId == mapping.personId)
                if person:
                    identity = {
                        "personId": person.uniqueId,
                        "name": person.name,
                        "admissionNumber": person.admissionNumber,
                    }
        except Exception:
            self.logger.exception("resolve_identity failed for hub_id=%s", hub_id)
            return None  # do not cache transient DB errors
        self._identity_cache[hub_id] = identity
        return identity

    def invalidate_identity(
        self, hub_id: Optional[int] = None, person_id: Optional[str] = None
    ) -> None:
        """Drop cache entries for a removed embedding (by hubId and/or personId)."""
        if hub_id is not None:
            self._identity_cache.pop(int(hub_id), None)
        if person_id is not None:
            for hid in [
                h
                for h, v in self._identity_cache.items()
                if v and v.get("personId") == person_id
            ]:
                self._identity_cache.pop(hid, None)

    def _draw_faces(self, frame, faces, names):
        """Draw thin detection boxes only — no name/confidence overlay.

        Identity text is surfaced to the UI via the recognized list returned by
        ``recognize_faces`` so it can be presented in a dedicated info panel.
        """
        for i, face in enumerate(faces):
            x1, y1, x2, y2 = face.location
            box = (int(x1), int(y1), int(x2), int(y2))
            name = names[i]
            color = (0, 220, 0) if name != "Unknown" else (0, 0, 255)
            cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), color, 1)
        return frame

    def recognize_faces(self, frame):
        """Process one frame and return ``(annotated_frame, recognized)``.

        ``recognized`` is a list of dicts (``name``, ``admissionNumber``) for the
        known faces in the frame, ordered by detection order. Unknown faces are
        still boxed but excluded from this list.
        """
        try:
            faces = self.session.face_detection(frame)
        except Exception as e:
            self.logger.exception(
                "face_detection failed - frame.shape=%sx%sx%s | error=%s",
                *frame.shape,
                e,
            )
            return frame, []

        names = []
        recognized: list[dict] = []

        if faces:
            for face in faces:
                x1, y1, x2, y2 = face.location
                self.logger.debug(
                    "Detected face - box=(%s,%s,%s,%s) w=%s h=%s",
                    x1,
                    y1,
                    x2,
                    y2,
                    x2 - x1,
                    y2 - y1,
                )

            for face in faces:
                feature = self.session.face_feature_extract(frame, face)
                if feature is None:
                    names.append("Unknown")
                    continue

                search_result = isf.feature_hub_face_search(feature)
                if not search_result or search_result.similar_identity.id == -1:
                    names.append("Unknown")
                    continue

                feature_id = search_result.similar_identity.id
                identity = self.resolve_identity(feature_id)
                if identity is None:
                    names.append("Unknown")
                    continue

                resolved_name = identity["name"]
                is_first_time = self.add_attendance_if_new(identity["personId"])
                if is_first_time and self.current_session_id:
                    try:
                        send_message(
                            {
                                "type": "person-recognized",
                                "sessionId": self.current_session_id,
                                "personId": identity["personId"],
                                "attendanceTimeStamp": ist_timestamp(),
                            }
                        )
                    except Exception:
                        self.logger.exception("send_message(person-recognized) failed")
                    try:
                        if self.on_first_attendance:
                            self.on_first_attendance(identity["personId"])
                    except Exception:
                        self.logger.exception("on_first_attendance callback failed")

                names.append(resolved_name)
                recognized.append(
                    {
                        "name": identity["name"],
                        "admissionNumber": identity["admissionNumber"],
                    }
                )

        return self._draw_faces(frame, faces, names), recognized

    def add_face(self, frame, person_id: Optional[str] = None):
        """Enroll the largest face in frame; returns (hub_id, feature) or None."""
        return enroll_from_image(self.session, frame, person_id)

    # --- Capture-based enrollment ----------------------------------------------

    @property
    def is_enrolling(self) -> bool:
        return self._enrollment is not None

    @property
    def is_enrollment_armed(self) -> bool:
        return self._enrollment is not None and self._enrollment.is_armed

    @property
    def is_enrollment_capturing(self) -> bool:
        return self._enrollment is not None and self._enrollment.is_capturing

    def arm_enrollment(self, person: dict) -> None:
        """Enter enrollment mode: show preview and wait for capture trigger."""
        self.logger.info("Enrollment armed for %s", person.get("personId"))
        self._enrollment = EnrollmentCapture(self.session, person)
        self._enrollment.arm()

    def start_enrollment_capture(self) -> None:
        """Begin collecting face samples (triggered from PWA)."""
        if not self._enrollment:
            raise RuntimeError("Enrollment is not armed")
        self.logger.info(
            "Enrollment capture started for %s", self._enrollment.person.get("personId")
        )
        self._enrollment.start_capture()

    def begin_enrollment(self, person: dict) -> None:
        """Legacy: arm and immediately start capture."""
        self.arm_enrollment(person)
        self.start_enrollment_capture()

    def cancel_enrollment(self) -> None:
        if self._enrollment:
            self._enrollment.disarm()
        self._enrollment = None

    def process_enrollment_frame(self, frame) -> Tuple[object, CaptureStatus]:
        """Run one frame through enrollment preview or capture."""
        status = self._enrollment.process_frame(frame)

        if status.face_location:
            x1, y1, x2, y2 = status.face_location
            if status.capturing:
                color = (0, 200, 0) if not status.failed else (0, 0, 255)
            else:
                color = (255, 200, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        name = self._enrollment.person.get("preferredName", "")
        if status.capturing:
            header = f"Capturing {name} [{status.accepted}/{status.required}]"
        else:
            header = f"Enrollment: {name}"

        cv2.putText(
            frame,
            header,
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 160, 0),
            2,
        )
        cv2.putText(
            frame,
            status.message,
            (10, 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255) if status.failed else (0, 200, 0),
            2,
        )

        if status.done or status.failed:
            self.logger.info(
                "Enrollment finished (done=%s failed=%s): %s",
                status.done,
                status.failed,
                status.message,
            )
            self._enrollment = None

        return frame, status
