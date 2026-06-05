"""Face enrollment helpers (InspireFace FeatureHub insert)."""

from __future__ import annotations

from typing import Any, Optional, Tuple

import inspireface as isf

from src.schema import FaceIdentityMap, db


def _largest_face(faces):
    return max(
        faces,
        key=lambda face: (face.location[2] - face.location[0])
        * (face.location[3] - face.location[1]),
    )


def enroll_from_image(
    session: Any,
    image_bgr: Any,
    person_id: Optional[str] = None,
) -> Optional[Tuple[int, object]]:
    """Detect the largest face, insert into FeatureHub, optionally map to person_id."""
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
            FaceIdentityMap.insert(hubId=hub_id, personId=person_id).on_conflict_replace().execute()
        finally:
            if not db.is_closed():
                db.close()

    return hub_id, feature
