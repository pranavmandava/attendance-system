from flask import Blueprint, jsonify

from src.schema import Session, db
from src.utils import python_string_to_timestamp

# Create a Blueprint for session routes
session_bp = Blueprint("session", __name__, url_prefix="/session")


def get_active_session_id():
    """Get the ID of the currently active session, or None if no active session."""
    try:
        active_session = Session.get_or_none(Session.actualEndTimestamp.is_null())
        return active_session.id if active_session else None
    except Exception:
        return None


@session_bp.before_request
def _open_db():
    """Ensure database connection is open before each request."""
    if db.is_closed():
        db.connect(reuse_if_open=True)


@session_bp.teardown_request
def _close_db(exc):
    """Close database connection after each request."""
    if not db.is_closed():
        db.close()


@session_bp.route("/", methods=["GET"])
def get_sessions():
    """Get all sessions."""
    try:
        sessions = Session.select().order_by(Session.startTimestamp.desc())
        session_list = []

        for session in sessions:
            session_list.append(
                {
                    "id": session.id,
                    "name": session.name,
                    "startTimestamp": session.startTimestamp.isoformat()
                    if session.startTimestamp
                    else None,
                    "plannedEndTimestamp": session.plannedEndTimestamp.isoformat()
                    if session.plannedEndTimestamp
                    else None,
                    "plannedDurationInMinutes": session.plannedDurationInMinutes,
                    "actualEndTimestamp": session.actualEndTimestamp.isoformat()
                    if session.actualEndTimestamp
                    else None,
                    "syncedAt": session.syncedAt.isoformat()
                    if session.syncedAt
                    else None,
                    "status": "active"
                    if session.actualEndTimestamp is None
                    else "ended",
                }
            )

        return jsonify({"sessions": session_list}), 200
    except Exception as e:
        return jsonify({"error": "Failed to fetch sessions", "details": str(e)}), 500


@session_bp.route("/current", methods=["GET"])
def get_current_session():
    """Return the current active session if present, else indicate inactive."""
    try:
        session = Session.get_or_none(Session.actualEndTimestamp.is_null())
        if not session:
            return jsonify({"active": False, "session": None}), 200

        payload = {
            "id": session.id,
            "name": session.name,
            "startTimestamp": python_string_to_timestamp(session.startTimestamp),
            "plannedEndTimestamp": python_string_to_timestamp(
                session.plannedEndTimestamp
            ),
            "plannedDurationInMinutes": session.plannedDurationInMinutes,
            "actualEndTimestamp": python_string_to_timestamp(
                session.actualEndTimestamp
            ),
            "syncedAt": python_string_to_timestamp(session.syncedAt),
        }

        return jsonify({"active": True, "session": payload}), 200
    except Exception as e:
        return (
            jsonify({"error": "Failed to fetch current session", "details": str(e)}),
            500,
        )
