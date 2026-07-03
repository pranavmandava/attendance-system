"""Confirm a completed kiosk enrollment to kcc-app."""

from __future__ import annotations

import os

from src.api.kcc_client import (
    KccDeviceClient,
    KccPermanentFailure,
    KccUnavailable,
    PERMANENT_FAILURE,
    get_kcc_client,
)
from src.config import ENROLLMENT_IMAGES_DIR
from src.schema import FaceIdentityMap, Person


def _upload_enrollment_image(client: KccDeviceClient, person: Person) -> None:
    """Best-effort upload of the kiosk snapshot to kcc-app object storage.

    A failure here must not undo a successful enrollment confirm, so errors
    are logged and swallowed rather than raised.
    """
    if not person.pictureFileName:
        print(f"[Enroll] No pictureFileName for {person.uniqueId} — skipping image")
        return

    image_path = os.path.join(ENROLLMENT_IMAGES_DIR, person.pictureFileName)
    if not os.path.exists(image_path):
        print(f"[Enroll] Snapshot missing on disk: {image_path}")
        return

    try:
        picture_url = client.upload_enrollment_image(person.uniqueId, image_path)
        print(f"[Enroll] Uploaded enrollment image for {person.uniqueId}: {picture_url}")
    except (KccUnavailable, KccPermanentFailure) as exc:
        print(f"[Enroll] Enrollment image upload failed for {person.uniqueId}: {exc}")


def confirm_enrollment_to_cloud(person_id: str) -> bool:
    """Push enrollment confirmation to kcc-app. Returns True on success."""
    client = get_kcc_client()
    if client is None:
        print("[Enroll] KCC client not configured — skipping confirm_enrollment")
        return False

    person = Person.get_or_none(Person.uniqueId == person_id)
    if not person:
        print(f"[Enroll] Person not found for confirm: {person_id}")
        return False

    mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.personId == person_id)
    if not mapping:
        print(f"[Enroll] FaceIdentityMap missing for confirm: {person_id}")
        return False

    try:
        resp = client.confirm_enrollment(person, str(mapping.hubId))
        person.syncedAt = resp.syncedAt
        person.error = None
        person.save()
        print(f"[Enroll] Confirmed enrollment for {person_id}")
        _upload_enrollment_image(client, person)
        return True
    except KccUnavailable as exc:
        print(f"[Enroll] confirm_enrollment unavailable (will retry): {exc}")
        return False
    except KccPermanentFailure as exc:
        person.syncedAt = PERMANENT_FAILURE
        person.error = str(exc)[:500]
        person.save()
        print(f"[Enroll] confirm_enrollment dead-lettered: {exc}")
        return False
