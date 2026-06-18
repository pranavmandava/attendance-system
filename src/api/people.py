from __future__ import annotations

import os

from flask import Blueprint, jsonify

from src.api.enrollment_state import get_session
from src.config import ENROLLMENT_IMAGES_DIR
from src.ipc import broadcast_message
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


@people_bp.route("/<person_id>", methods=["DELETE"])
def delete_person(person_id: str):
    mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.personId == person_id)

    # The kiosk UI owns the InspireFace FeatureHub; ask it to drop the
    # embedding. If no UI is connected the stale feature resolves to
    # "Unknown" (its mapping row is gone) until re-enrollment overwrites it.
    if mapping:
        broadcast_message(
            {
                "type": "unenroll-request",
                "personId": person_id,
                "hubId": mapping.hubId,
            }
        )
        mapping.delete_instance()

    try:
        person = Person.get_or_none(Person.uniqueId == person_id)
        if person:
            image_path = os.path.join(ENROLLMENT_IMAGES_DIR, person.pictureFileName)
            try:
                if os.path.exists(image_path):
                    os.remove(image_path)
            except OSError:
                pass
            person.delete_instance()
    except Exception as e:
        return jsonify({"error": "Failed to delete person", "details": str(e)}), 500

    return jsonify({"deleted": True, "personId": person_id}), 200
