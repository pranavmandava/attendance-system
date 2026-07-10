"""Enrollment HTTP routes: prepare → capture → poll status."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from src.api.enrollment_state import (
    clear_session,
    get_session,
    session_to_dict,
    set_phase,
    upsert_prepare,
)
from src.api.enroll_confirm import confirm_enrollment_to_cloud
from src.ipc import broadcast_message

enroll_bp = Blueprint("enroll", __name__)

__all__ = ["enroll_bp", "confirm_enrollment_to_cloud"]


def _validate_person_payload(data: dict) -> tuple[dict | None, tuple | None]:
    missing = [k for k in ("personId", "preferredName", "userType") if not data.get(k)]
    if missing:
        return None, (
            jsonify({"message": "Missing required fields", "missing": missing}),
            400,
        )
    if data["userType"] not in ("Cadet", "Employee"):
        return None, (
            jsonify({"message": "userType must be Cadet or Employee"}),
            400,
        )
    if data["userType"] == "Cadet" and not data.get("admissionNumber"):
        return None, (
            jsonify({"message": "admissionNumber is required for Cadet"}),
            400,
        )

    person = {
        "personId": data["personId"],
        "preferredName": data["preferredName"],
        "admissionNumber": data.get("admissionNumber"),
        "roomId": data.get("roomId"),
        "roomName": data.get("roomName"),
        "bedNumber": data.get("bedNumber"),
        "userType": data["userType"],
    }
    return person, None


@enroll_bp.route("/prepare", methods=["POST"])
def enroll_prepare():
    """Send student metadata to the kiosk and arm enrollment preview mode."""
    data = request.json or {}
    person, error = _validate_person_payload(data)
    if error:
        return error

    session = upsert_prepare(
        admission_number=person.get("admissionNumber"),
        bed_number=person.get("bedNumber"),
        person_id=person["personId"],
        preferred_name=person["preferredName"],
        room_id=person.get("roomId"),
        user_type=person["userType"],
    )

    clients = broadcast_message({"type": "enroll-prepare", **person})
    if clients == 0:
        clear_session(person["personId"])
        return (
            jsonify({"message": "Kiosk UI is not connected"}),
            503,
        )

    return (
        jsonify(
            {
                "message": "Kiosk ready for capture",
                "personId": person["personId"],
                **session_to_dict(session),
            }
        ),
        202,
    )


@enroll_bp.route("/capture", methods=["POST"])
def enroll_capture():
    """Trigger face sample collection on the kiosk (PWA Capture button)."""
    data = request.json or {}
    person_id = data.get("personId")
    if not person_id:
        return jsonify({"message": "personId is required"}), 400

    session = get_session(person_id)
    if not session:
        return jsonify({"message": "No prepared enrollment for this personId"}), 404

    set_phase(person_id, "capturing", "Capturing face samples")
    clients = broadcast_message({"type": "enroll-capture", "personId": person_id})
    if clients == 0:
        set_phase(person_id, "ready", "Kiosk disconnected")
        return (
            jsonify({"message": "Kiosk UI is not connected"}),
            503,
        )

    return (
        jsonify(
            {
                "message": "Capture started on kiosk",
                "personId": person_id,
                "phase": "capturing",
            }
        ),
        202,
    )


@enroll_bp.route("/cancel", methods=["POST"])
def enroll_cancel():
    data = request.json or {}
    person_id = data.get("personId")
    if not person_id:
        return jsonify({"message": "personId is required"}), 400

    broadcast_message({"type": "enroll-cancel", "personId": person_id})
    clear_session(person_id)

    return jsonify({"message": "Enrollment cancelled", "personId": person_id}), 200


@enroll_bp.route("", methods=["POST"])
@enroll_bp.route("/", methods=["POST"])
def enroll_legacy():
    """Backward-compatible alias: prepare only (does not auto-capture)."""
    return enroll_prepare()
