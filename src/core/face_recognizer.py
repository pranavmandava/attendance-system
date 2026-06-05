import platform
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

import cv2
import inspireface as isf

from src.config import INSPIREFACE_MODEL_NAME
from src.core.enrollment import enroll_from_image
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

    def _draw_faces(self, frame, faces, names, confidences):
        for i, face in enumerate(faces):
            x1, y1, x2, y2 = face.location
            box = (int(x1), int(y1), int(x2), int(y2))
            name = names[i]
            confidence = confidences[i]
            color = (0, 255, 0) if name != "Unknown" else (0, 0, 255)

            cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), color, 2)

            text = f"{name}: {confidence:.2f}" if name != "Unknown" else name
            cv2.putText(
                frame,
                text,
                (box[0], box[1] - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
            )
        return frame

    def recognize_faces(self, frame):
        try:
            faces = self.session.face_detection(frame)
        except Exception as e:
            self.logger.exception(
                "face_detection failed - frame.shape=%sx%sx%s | error=%s",
                *frame.shape,
                e,
            )
            return frame

        names = []
        confidences = []

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
                    confidences.append(0.0)
                    continue

                search_result = isf.feature_hub_face_search(feature)
                if not search_result or search_result.similar_identity.id == -1:
                    names.append("Unknown")
                    confidences.append(0.0)
                    continue

                feature_id = search_result.similar_identity.id
                resolved_name = "Unknown"
                try:
                    if db.is_closed():
                        db.connect(reuse_if_open=True)
                    mapping = FaceIdentityMap.get_or_none(
                        FaceIdentityMap.hubId == feature_id
                    )
                    if mapping:
                        person = Person.get_or_none(
                            Person.uniqueId == mapping.personId
                        )
                        if person:
                            resolved_name = person.name
                            is_first_time = self.add_attendance_if_new(person.uniqueId)

                            if is_first_time and self.current_session_id:
                                try:
                                    send_message(
                                        {
                                            "type": "person-recognized",
                                            "sessionId": self.current_session_id,
                                            "personId": person.uniqueId,
                                            "attendanceTimeStamp": ist_timestamp(),
                                        }
                                    )
                                except Exception:
                                    pass
                                try:
                                    if self.on_first_attendance:
                                        self.on_first_attendance(person.uniqueId)
                                except Exception:
                                    pass
                finally:
                    if not db.is_closed():
                        db.close()

                names.append(resolved_name)
                confidences.append(search_result.confidence)

        return self._draw_faces(frame, faces, names, confidences)

    def add_face(self, frame, person_id: Optional[str] = None):
        """Enroll the largest face in frame; returns (hub_id, feature) or None."""
        return enroll_from_image(self.session, frame, person_id)
