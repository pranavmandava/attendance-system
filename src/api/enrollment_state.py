"""In-memory enrollment session state (prepare → capture → done)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

EnrollmentPhase = Literal["idle", "ready", "capturing", "completed", "failed"]


@dataclass
class EnrollmentSession:
    person_id: str
    preferred_name: str
    user_type: str
    admission_number: Optional[str] = None
    room_id: Optional[str] = None
    bed_number: Optional[int] = None
    phase: EnrollmentPhase = "ready"
    message: str = "Waiting for capture"
    updated_at: datetime = field(default_factory=datetime.now)


_sessions: dict[str, EnrollmentSession] = {}


def upsert_prepare(
    *,
    person_id: str,
    preferred_name: str,
    user_type: str,
    admission_number: Optional[str] = None,
    room_id: Optional[str] = None,
    bed_number: Optional[int] = None,
) -> EnrollmentSession:
    session = EnrollmentSession(
        admission_number=admission_number,
        bed_number=bed_number,
        person_id=person_id,
        preferred_name=preferred_name,
        phase="ready",
        room_id=room_id,
        user_type=user_type,
        message="Kiosk ready — press Capture in the app",
    )
    _sessions[person_id] = session
    return session


def get_session(person_id: str) -> Optional[EnrollmentSession]:
    return _sessions.get(person_id)


def set_phase(
    person_id: str,
    phase: EnrollmentPhase,
    message: Optional[str] = None,
) -> Optional[EnrollmentSession]:
    session = _sessions.get(person_id)
    if not session:
        return None
    session.phase = phase
    if message is not None:
        session.message = message
    session.updated_at = datetime.now()
    return session


def clear_session(person_id: str) -> None:
    _sessions.pop(person_id, None)


def session_to_dict(session: EnrollmentSession) -> dict:
    return {
        "admissionNumber": session.admission_number,
        "bedNumber": session.bed_number,
        "message": session.message,
        "personId": session.person_id,
        "phase": session.phase,
        "preferredName": session.preferred_name,
        "roomId": session.room_id,
        "updatedAt": session.updated_at.isoformat(),
        "userType": session.user_type,
    }
