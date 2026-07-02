from __future__ import annotations

from flask import Blueprint, jsonify

from src.api.enrollment_state import get_session
from src.schema import FaceIdentityMap, Person, db

people_bp = Blueprint("people", __name__, url_prefix="/people")


@people_bp.before_request
def _open_db():
    if db.is_closed():
        db.connect(reuse_if_open=True)


@people_bp.teardown_request
def _close_db(exc):
    if not db.is_closed():
        db.close()


@people_bp.route("/<person_id>/enrollment-status", methods=["GET"])
def enrollment_status(person_id: str):
    """Report whether a capture enrollment (started via /enroll) has landed.

    The kiosk UI writes the Person row and the FeatureHub mapping when the
    capture completes, so both existing means the person is recognizable.
    """
    person = Person.get_or_none(Person.uniqueId == person_id)
    mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.personId == person_id)
    session = get_session(person_id)
    enrolled = person is not None and mapping is not None

    if enrolled:
        phase = "completed"
    elif session:
        phase = session.phase
    else:
        phase = "idle"

    return jsonify(
        {
            "personId": person_id,
            "enrolled": enrolled,
            "name": person.name if person else (session.preferred_name if session else None),
            "phase": phase,
            "message": session.message if session else None,
            "syncedAt": person.syncedAt if person else None,
        }
    ), 200
