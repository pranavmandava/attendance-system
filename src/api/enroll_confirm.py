"""Confirm a completed kiosk enrollment to kcc-app."""

from __future__ import annotations

from src.api.kcc_client import (
    KccPermanentFailure,
    KccUnavailable,
    PERMANENT_FAILURE,
    get_kcc_client,
)
from src.schema import FaceIdentityMap, Person


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
