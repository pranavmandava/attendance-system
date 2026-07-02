"""Face enrollment (capture-based, InspireFace FeatureHub).

Enrollment runs against the live kiosk camera feed - the same camera,
position and lighting used for recognition - instead of uploaded photos.
Several quality-gated samples are collected and averaged into a single
embedding before insertion into FeatureHub.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import cv2
import inspireface as isf
import numpy as np

from src.config import (
    ENROLL_CENTER_TOLERANCE,
    ENROLL_MIN_FACE_WIDTH,
    ENROLL_MIN_SAMPLE_GAP_SECONDS,
    ENROLL_MIN_SELF_SIMILARITY,
    ENROLL_MIN_SHARPNESS,
    ENROLL_SAMPLES_REQUIRED,
    ENROLL_TIMEOUT_SECONDS,
    ENROLLMENT_IMAGES_DIR,
)
from src.schema import FaceIdentityMap, Person, db


def _largest_face(faces):
    return max(
        faces,
        key=lambda face: (face.location[2] - face.location[0])
        * (face.location[3] - face.location[1]),
    )


def _normalize(feature: Any) -> np.ndarray:
    vec = np.asarray(feature, dtype=np.float32).flatten()
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def enroll_from_image(
    session: Any,
    image_bgr: Any,
    person_id: Optional[str] = None,
) -> Optional[Tuple[int, object]]:
    """Detect the largest face, insert into FeatureHub, optionally map to person_id.

    Kept for offline/bulk imports; the kiosk uses EnrollmentCapture instead.
    """
    faces = session.face_detection(image_bgr)
    if not faces:
        return None

    feature = session.face_feature_extract(image_bgr, _largest_face(faces))
    if feature is None:
        return None

    identity = isf.FaceIdentity(feature, id=-1)
    ret, hub_id = isf.feature_hub_face_insert(identity)
    if not ret:
        return None

    if person_id:
        try:
            if db.is_closed():
                db.connect(reuse_if_open=True)
            FaceIdentityMap.insert(
                hubId=hub_id, personId=person_id
            ).on_conflict_replace().execute()
        finally:
            if not db.is_closed():
                db.close()

    return hub_id, feature


@dataclass
class CaptureStatus:
    """Per-frame feedback for the kiosk UI during capture enrollment."""

    accepted: int
    required: int
    message: str
    face_location: Optional[Tuple[int, int, int, int]] = None
    done: bool = False
    failed: bool = False
    person: Optional[dict] = None
    armed: bool = False
    capturing: bool = False


class EnrollmentCapture:
    """Collects quality-gated face samples from the live feed for one person.

    Two-step flow:
    1. arm() — store metadata and show a live preview (single-face gate only).
    2. start_capture() — collect ENROLL_SAMPLES_REQUIRED shots when PWA triggers.
    """

    def __init__(self, session: Any, person: dict):
        self.session = session
        self.person = person
        self.features: list[np.ndarray] = []
        self.best_frame = None
        self.best_sharpness = 0.0
        self.last_sample_at = 0.0
        self._armed = False
        self._capturing = False
        self._capture_started_at = 0.0

    @property
    def is_armed(self) -> bool:
        return self._armed and not self._capturing

    @property
    def is_capturing(self) -> bool:
        return self._capturing

    def arm(self) -> None:
        self._armed = True
        self._capturing = False
        self.features = []
        self.best_frame = None
        self.best_sharpness = 0.0
        self.last_sample_at = 0.0

    def start_capture(self) -> None:
        if not self._armed:
            self.arm()
        self._capturing = True
        self._capture_started_at = time.monotonic()
        self.features = []
        self.best_frame = None
        self.best_sharpness = 0.0
        self.last_sample_at = 0.0

    def disarm(self) -> None:
        self._armed = False
        self._capturing = False

    def process_frame(self, frame) -> CaptureStatus:
        if self._capturing:
            return self._process_capturing(frame)
        if self._armed:
            return self._process_preview(frame)
        return self._status("Enrollment not armed")

    def _process_preview(self, frame) -> CaptureStatus:
        """Live preview while waiting for the PWA capture button."""
        location, hint = self._detect_single_face(frame)
        if hint:
            return self._status(hint, location, armed=True)
        return self._status(
            f"Ready: {self.person.get('preferredName', '')} — press Capture in app",
            location,
            armed=True,
        )

    def _process_capturing(self, frame) -> CaptureStatus:
        if time.monotonic() - self._capture_started_at > ENROLL_TIMEOUT_SECONDS:
            return self._status("Capture timed out", failed=True, capturing=True)

        location, hint = self._detect_single_face(frame)
        if hint:
            return self._status(hint, location, capturing=True)

        face = self._largest_face(self.session.face_detection(frame))
        x1, y1, x2, y2 = (int(v) for v in face.location)
        location = (x1, y1, x2, y2)

        frame_h, frame_w = frame.shape[:2]
        if (x2 - x1) < ENROLL_MIN_FACE_WIDTH:
            return self._status("Move closer", location, capturing=True)

        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if (
            abs(cx - frame_w / 2) > frame_w * ENROLL_CENTER_TOLERANCE
            or abs(cy - frame_h / 2) > frame_h * ENROLL_CENTER_TOLERANCE
        ):
            return self._status("Center your face", location, capturing=True)

        crop = frame[max(y1, 0) : max(y2, 1), max(x1, 0) : max(x2, 1)]
        if crop.size == 0:
            return self._status("Center your face", location, capturing=True)
        sharpness = cv2.Laplacian(
            cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F
        ).var()
        if sharpness < ENROLL_MIN_SHARPNESS:
            return self._status("Hold still", location, capturing=True)

        now = time.monotonic()
        if now - self.last_sample_at < ENROLL_MIN_SAMPLE_GAP_SECONDS:
            return self._status("Capturing...", location, capturing=True)

        feature = self.session.face_feature_extract(frame, face)
        if feature is None:
            return self._status("Hold still", location, capturing=True)
        feature = _normalize(feature)

        if self.features:
            mean = _normalize(np.mean(self.features, axis=0))
            if float(np.dot(mean, feature)) < ENROLL_MIN_SELF_SIMILARITY:
                return self._status(
                    "Stay in frame - sample mismatch", location, capturing=True
                )

        self.features.append(feature)
        self.last_sample_at = now
        if sharpness > self.best_sharpness:
            self.best_sharpness = sharpness
            self.best_frame = frame.copy()

        if len(self.features) >= ENROLL_SAMPLES_REQUIRED:
            return self._finalize(location)

        return self._status(
            f"Captured {len(self.features)}/{ENROLL_SAMPLES_REQUIRED}",
            location,
            capturing=True,
        )

    def _detect_single_face(self, frame):
        try:
            faces = self.session.face_detection(frame)
        except Exception as exc:
            return None, f"Detection error: {exc}"

        if not faces:
            return None, "Look at the camera"
        if len(faces) > 1:
            return None, "Only one person in frame, please"

        face = faces[0]
        x1, y1, x2, y2 = (int(v) for v in face.location)
        return (x1, y1, x2, y2), None

    @staticmethod
    def _largest_face(faces):
        return _largest_face(faces)

    # --- finalization -----------------------------------------------------------

    def _finalize(self, location) -> CaptureStatus:
        person_id = self.person["personId"]
        mean_feature = _normalize(np.mean(self.features, axis=0))

        try:
            if db.is_closed():
                db.connect(reuse_if_open=True)

            # Refuse to enroll a face that already matches a different person
            search_result = isf.feature_hub_face_search(mean_feature)
            if search_result and search_result.similar_identity.id != -1:
                existing = FaceIdentityMap.get_or_none(
                    FaceIdentityMap.hubId == search_result.similar_identity.id
                )
                if existing and existing.personId != person_id:
                    other = Person.get_or_none(Person.uniqueId == existing.personId)
                    other_name = other.name if other else existing.personId
                    return self._status(
                        f"Face already enrolled as {other_name}", location, failed=True
                    )

            # Re-enrollment: drop the previous embedding for this person
            previous = FaceIdentityMap.get_or_none(
                FaceIdentityMap.personId == person_id
            )
            if previous:
                try:
                    isf.feature_hub_face_remove(int(previous.hubId))
                except Exception:
                    pass
                previous.delete_instance()

            ret, hub_id = isf.feature_hub_face_insert(
                isf.FaceIdentity(mean_feature, id=-1)
            )
            if not ret:
                return self._status("FeatureHub insert failed", location, failed=True)

            snapshot_name = f"{person_id}.jpg"
            cv2.imwrite(
                str(ENROLLMENT_IMAGES_DIR / snapshot_name),
                self.best_frame if self.best_frame is not None else np.zeros((1, 1, 3)),
            )

            Person.insert(
                uniqueId=person_id,
                name=self.person["preferredName"],
                admissionNumber=self.person.get("admissionNumber"),
                roomId=self.person.get("roomId"),
                pictureFileName=snapshot_name,
                personType=self.person["userType"],
                syncedAt=None,
            ).on_conflict_replace().execute()
            FaceIdentityMap.insert(
                hubId=hub_id, personId=person_id
            ).on_conflict_replace().execute()
        except Exception as exc:
            return self._status(f"Enrollment failed: {exc}", location, failed=True)
        finally:
            if not db.is_closed():
                db.close()

        return self._status(
            f"Enrolled {self.person['preferredName']}", location, done=True
        )

    def _status(
        self,
        message: str,
        location=None,
        done: bool = False,
        failed: bool = False,
        armed: bool = False,
        capturing: bool = False,
    ) -> CaptureStatus:
        return CaptureStatus(
            accepted=len(self.features),
            required=ENROLL_SAMPLES_REQUIRED,
            message=message,
            face_location=location,
            done=done,
            failed=failed,
            person=self.person,
            armed=armed or self.is_armed,
            capturing=capturing or self.is_capturing,
        )
