import os
from datetime import datetime
from os import uname

import requests as req
from flask import Flask, request
from flask_cors import CORS
from uuid_extension import uuid7

from src.api.connectivity import connectivity_bp
from src.api.enroll import enroll_bp
from src.api.enrollment_state import get_session, set_phase
from src.api.people import people_bp
from src.api.session import get_active_session_id, session_bp
from src.ipc import (
    add_message_handler,
    broadcast_message,
    start_socket_server,
    stop_socket_server,
)
from src.schema import CadetAttendance, Person, Room, db, ensure_db_schema
from src.utils import ist_timestamp, string_to_timestamp

app = Flask(__name__)
_default_cors_origins = [
    "http://localhost:8787",
    "https://api.korukondacoachingcentre.com",
    "https://korukondacoachingcentre.com",
    "https://app.korukondacoachingcentre.com",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]
_extra_cors_origins = [
    origin.strip()
    for origin in os.environ.get("AXON_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
_cors_origins = _default_cors_origins + _extra_cors_origins
CORS(
    app,
    resources={
        r"/*": {
            "allow_private_network": True,
            "origins": _cors_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
            "allow_headers": [
                "Content-Type",
                "Access-Control-Request-Private-Network",
            ],
        }
    },
)

# Ensure database tables exist on startup
ensure_db_schema()

# Register Blueprints
app.register_blueprint(session_bp)
app.register_blueprint(people_bp)
app.register_blueprint(enroll_bp, url_prefix="/enroll")
app.register_blueprint(connectivity_bp)


@app.before_request
def _open_db():
    if db.is_closed():
        db.connect(reuse_if_open=True)


@app.teardown_request
def _close_db(exc):
    if not db.is_closed():
        db.close()


@app.route("/")
def hello_world():
    return {
        "message": "Hello, World!",
        "machine": uname(),
        "timestamp": datetime.now().isoformat(),
    }


@app.route("/test", methods=["GET"])
def test():
    return {"uuid": uuid7()}


# Legacy route kept for older clients — delegates to prepare-only flow.
@app.route("/enroll", methods=["POST"])
def enroll_legacy_redirect():
    from src.api.enroll import enroll_prepare

    return enroll_prepare()


@app.route("/ipc/send", methods=["POST"])
def ipc_send():
    """Broadcast a message received via HTTP to all connected UI clients."""
    data = request.json or {}
    if not data:
        return {"error": "No payload provided"}, 400

    # Validate that it's a dict
    if not isinstance(data, dict):
        return {"error": "Payload must be a JSON object"}, 400

    broadcast_message(data)
    return {"status": "Message broadcasted", "payload": data}, 200


@app.route("/rooms", methods=["GET"])
def list_rooms():
    rooms = [
        {"roomId": room.roomId, "roomName": room.roomName}
        for room in Room.select().order_by(Room.roomName.asc())
    ]
    return {"rooms": rooms}, 200


@app.route("/setup-rooms", methods=["POST"])
def setup_rooms():
    rooms = [
        {
            "unique_id": "01978221-6a29-70f0-99f0-996d856ecf47",
            "room_name": "Jhansi",
            "warden_name": None,
            "created_at": "2025-06-18 08:21:57.417",
            "updated_at": "2025-06-18 08:21:57.426",
            "gender": "Female",
            "place": "0-Left",
        },
        {
            "unique_id": "01978221-769d-768f-894e-e833a9cbcfd4",
            "room_name": "Aakash",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:00.605",
            "updated_at": "2025-06-18 08:22:00.607",
            "gender": "Male",
            "place": "1-Left",
        },
        {
            "unique_id": "01978221-819e-734c-a2f0-7e58b7ebd42d",
            "room_name": "Prithvi",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:03.422",
            "updated_at": "2025-06-18 08:22:03.423",
            "gender": "Male",
            "place": "1-Right",
        },
        {
            "unique_id": "01978221-87db-750a-b10e-119bdc4b88be",
            "room_name": "Viraat",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:05.019",
            "updated_at": "2025-06-18 08:22:05.021",
            "gender": "Male",
            "place": "2-Left",
        },
        {
            "unique_id": "01978221-8f44-70d6-88f3-b459ac5273df",
            "room_name": "Sindhurakshak",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:06.916",
            "updated_at": "2025-06-18 08:22:06.918",
            "gender": "Male",
            "place": "2-Right",
        },
        {
            "unique_id": "01978221-9589-726c-98d8-64b502ca02dc",
            "room_name": "Tejas",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:08.521",
            "updated_at": "2025-06-18 08:22:08.522",
            "gender": "Male",
            "place": "3-Left",
        },
        {
            "unique_id": "01978221-9cf8-72ea-bbf5-1ad2a85b5393",
            "room_name": "Cheetah",
            "warden_name": None,
            "created_at": "2025-06-18 08:22:10.424",
            "updated_at": "2025-06-18 08:22:10.426",
            "gender": "Male",
            "place": "3-Right",
        },
    ]

    try:
        for room in rooms:
            Room.insert(
                roomId=room["unique_id"],
                roomName=room["room_name"],
                syncedAt=ist_timestamp(),
            ).on_conflict_replace().execute()
    except Exception as e:
        print(e)
        return {"message": "Failed to setup rooms", "error": str(e)}, 500

    return {"message": "Rooms setup successfully"}, 200


# ---------------------------------------------------------------------------
# IPC message handling
# ---------------------------------------------------------------------------


def _mark_attendance_remote(person_id: str, attendanceTimeStamp: str, session_id: str):
    """Send attendance mark request to the remote Axon API and return the
    `syncedAt` timestamp if the call is successful. Returns None on failure.
    """

    print("[Flask] Sending data to kcc api")
    url = "http://api.korukondacoachingcentre.com/axon/mark-attendance"
    try:
        resp = req.post(
            url,
            json={
                "sessionId": session_id,
                "personId": person_id,
                "attendanceTimeStamp": attendanceTimeStamp,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(
                f"[Attendance] Remote API error {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json()

        print("[Flask] Got Response from KCC API", data.get("syncedAt"))
        return data.get("syncedAt")
    except Exception as exc:
        print(f"[Attendance] Remote API request failed: {exc}")
        return None


def _handle_ipc_message(payload: dict):
    """Callback invoked for every message received over the IPC socket."""
    # The entire payload is the message - no nested keys to look for
    print(f"[Attendance] Received payload: {payload}")

    # Check message type and handle accordingly
    message_type = payload.get("type")

    if message_type == "person-recognized":
        person_id = payload.get("personId")
        attendanceTimeStamp = payload.get("attendanceTimeStamp")
        session_id = payload.get("sessionId")
        if not person_id or not attendanceTimeStamp or not session_id:
            print(
                "[Attendance] person-recognized payload missing fields",
                {
                    "personId": bool(person_id),
                    "attendanceTimeStamp": bool(attendanceTimeStamp),
                    "sessionId": bool(session_id),
                },
            )
            return

        # Call remote API
        synced_at_iso = _mark_attendance_remote(
            person_id, attendanceTimeStamp, session_id
        )

        # Persist to local DB regardless of remote sync success
        try:
            if db.is_closed():
                db.connect(reuse_if_open=True)

            CadetAttendance.insert(
                personId=person_id,
                attendanceTimeStamp=string_to_timestamp(attendanceTimeStamp),
                sessionId=session_id,
                syncedAt=string_to_timestamp(synced_at_iso),
            ).execute()
            # Broadcast attendance event to UI clients
            try:
                person = Person.get_or_none(Person.uniqueId == person_id)
                broadcast_message(
                    {
                        "type": "attendance",
                        "personId": person_id,
                        "name": person.name if person else None,
                        "syncedAt": string_to_timestamp(synced_at_iso),
                    }
                )
            except Exception:
                pass
        except Exception as exc:
            print(f"[Attendance] DB write error: {exc}")

    elif message_type == "enrollment-result":
        person_id = payload.get("personId")
        status = payload.get("status")
        message = payload.get("message")
        if person_id:
            if status == "completed":
                set_phase(person_id, "completed", message or "Enrolled")
            elif status == "failed":
                set_phase(person_id, "failed", message or "Capture failed")
        print(
            f"[Enrollment] {status}: "
            f"{payload.get('name')} ({person_id}) - "
            f"{message}"
        )
        broadcast_message(payload)

    elif message_type == "user-action":
        # Handle user actions from UI
        action = payload.get("action")
        if action == "enroll":
            # Handle enrollment request
            print(f"[Server] Enrollment request: {payload}")
        elif action == "test":
            # Handle test request
            print(f"[Server] Test request: {payload}")

    else:
        # Handle other message types
        print(f"[Server] Unknown message type: {message_type}")
        print(f"[Server] Full payload: {payload}")


# Register the IPC message handler
add_message_handler(_handle_ipc_message)


if __name__ == "__main__":
    # Start the socket server for IPC communication
    start_socket_server()

    try:
        app.run(debug=True, host="0.0.0.0", port=1337)
    finally:
        # Cleanup socket server on exit
        stop_socket_server()
