"""SSE consumer for cloud→device commands (delete, session lifecycle)."""

from __future__ import annotations

import os
import threading
import time

from src.api.kcc_client import (
    CommandAckIn,
    CommandEnvelope,
    EndSessionCommand,
    KccPermanentFailure,
    KccUnavailable,
    StartSessionCommand,
    get_kcc_client,
)
from src.config import ENROLLMENT_IMAGES_DIR
from src.ipc import broadcast_message
from src.schema import FaceIdentityMap, Person, Session, SyncCursor, db
from src.utils import ist_timestamp, string_to_timestamp

PING_MISS_LIMIT = 3
PING_TIMEOUT_S = 45.0
SYNC_HEARTBEAT_S = 3600.0
LAST_EVENT_CURSOR_KEY = "lastCommandEventId"

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _get_last_event_id() -> int | None:
    cursor = SyncCursor.get_or_none(SyncCursor.key == LAST_EVENT_CURSOR_KEY)
    if not cursor or not cursor.value:
        return None
    try:
        return int(cursor.value)
    except ValueError:
        return None


def _set_last_event_id(event_id: int) -> None:
    SyncCursor.insert(
        key=LAST_EVENT_CURSOR_KEY,
        value=str(event_id),
    ).on_conflict_replace().execute()


def _ack_command(client, command_id: str, status: str, detail: str | None = None) -> None:
    client.ack_command(
        CommandAckIn(
            deviceId=client.device_id,
            commandId=command_id,
            status=status,  # type: ignore[arg-type]
            appliedAt=ist_timestamp(),
            detail=detail,
        )
    )


def _delete_person_local(cadet_id: str, hub_id: str | None) -> None:
    broadcast_message(
        {
            "type": "delete-embedding",
            "personId": cadet_id,
            "cadetId": cadet_id,
            "hubId": hub_id,
        }
    )

    mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.personId == cadet_id)
    if mapping:
        mapping.delete_instance()

    person = Person.get_or_none(Person.uniqueId == cadet_id)
    if person:
        image_path = os.path.join(ENROLLMENT_IMAGES_DIR, person.pictureFileName)
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except OSError:
            pass
        person.delete_instance()


def _handle_start_session(cmd: StartSessionCommand) -> None:
    start_timestamp = string_to_timestamp(cmd.startTimestamp)
    Session.insert(
        id=cmd.sessionId,
        name=cmd.name,
        startTimestamp=start_timestamp,
        plannedEndTimestamp=string_to_timestamp(cmd.plannedEndTimestamp),
        plannedDurationInMinutes=cmd.plannedDurationInMinutes,
        actualEndTimestamp=None,
        syncedAt=ist_timestamp(),
    ).on_conflict_replace().execute()

    # Superseded sessions end when the new one starts — same rule as the cloud.
    Session.update(actualEndTimestamp=start_timestamp).where(
        (Session.id != cmd.sessionId) & (Session.actualEndTimestamp.is_null())
    ).execute()

    broadcast_message(
        {
            "type": "start-session",
            "sessionId": cmd.sessionId,
            "name": cmd.name,
            "startTimestamp": cmd.startTimestamp,
            "plannedEndTimestamp": cmd.plannedEndTimestamp,
            "plannedDurationInMinutes": cmd.plannedDurationInMinutes,
        }
    )


def _handle_end_session(cmd: EndSessionCommand) -> None:
    actual_end = string_to_timestamp(cmd.actualEndTimestamp)
    Session.update(actualEndTimestamp=actual_end).where(
        Session.id == cmd.sessionId
    ).execute()

    broadcast_message(
        {
            "type": "end-session",
            "sessionId": cmd.sessionId,
            "actualEndTimestamp": cmd.actualEndTimestamp,
        }
    )


def _dispatch_command(client, command: CommandEnvelope) -> tuple[str, str | None]:
    """Apply a command locally. Returns (ack_status, detail)."""
    if command.kind == "delete-embedding":
        _delete_person_local(command.cadetId, command.hubId)
        return "applied", None

    if command.kind == "unenroll":
        _delete_person_local(command.cadetId, None)
        return "applied", None

    if command.kind == "rename-person":
        person = Person.get_or_none(Person.uniqueId == command.cadetId)
        if person:
            person.name = command.preferredName
            person.save()
        return "applied", None

    if command.kind == "start-session":
        _handle_start_session(command)  # type: ignore[arg-type]
        return "applied", None

    if command.kind == "end-session":
        _handle_end_session(command)  # type: ignore[arg-type]
        return "applied", None

    if command.kind == "ping":
        return "applied", None

    return "refused", f"Unknown command kind: {getattr(command, 'kind', '?')}"


def _run_sync_heartbeat(client) -> None:
    if db.is_closed():
        db.connect(reuse_if_open=True)

    local_cadet_ids = [p.uniqueId for p in Person.select()]
    local_hub_ids = [str(m.hubId) for m in FaceIdentityMap.select()]

    try:
        resp = client.sync_commands(local_cadet_ids, local_hub_ids)
    except (KccUnavailable, KccPermanentFailure) as exc:
        print(f"[CommandStream] sync_commands heartbeat failed: {exc}")
        return

    for item in resp.deleteThese:
        print(f"[CommandStream] Reconcile delete cadet={item.cadetId}")
        _delete_person_local(item.cadetId, item.hubId)


def _consume_stream(client) -> None:
    last_event_id = _get_last_event_id()
    last_ping_at = time.monotonic()
    missed_pings = 0
    last_sync_at = time.monotonic()

    while not _stop_event.is_set():
        try:
            for event_id, event_type, command in client.open_command_stream(last_event_id):
                if _stop_event.is_set():
                    break

                # Pings arrive every 15s, so this fires even on a quiet stream.
                if time.monotonic() - last_sync_at >= SYNC_HEARTBEAT_S:
                    _run_sync_heartbeat(client)
                    last_sync_at = time.monotonic()

                if event_type == "resync":
                    print("[CommandStream] Resync requested — running sync_commands")
                    _run_sync_heartbeat(client)
                    last_sync_at = time.monotonic()
                    continue

                if command is None:
                    continue

                if command.kind == "ping":
                    last_ping_at = time.monotonic()
                    missed_pings = 0
                    if event_id is not None:
                        last_event_id = event_id
                        _set_last_event_id(event_id)
                    continue

                print(f"[CommandStream] Received {command.kind} commandId={command.commandId}")
                try:
                    status, detail = _dispatch_command(client, command)
                except Exception as exc:
                    # A command that fails locally must not wedge the stream:
                    # ack it as failed and move the cursor past it.
                    status, detail = "failed", f"{type(exc).__name__}: {exc}"[:500]
                    print(f"[CommandStream] Dispatch failed for {command.commandId}: {detail}")

                try:
                    _ack_command(client, command.commandId, status, detail)
                    print(f"[CommandStream] Acked {command.commandId} status={status}")
                except KccUnavailable as exc:
                    # Cursor not advanced — the command is redelivered on reconnect.
                    print(f"[CommandStream] Ack unavailable, will retry: {exc}")
                    break
                except KccPermanentFailure as exc:
                    print(f"[CommandStream] Ack rejected by cloud: {exc}")

                if event_id is not None:
                    last_event_id = event_id
                    _set_last_event_id(event_id)

            # Stream ended — check ping health before reconnect
            if time.monotonic() - last_ping_at > PING_TIMEOUT_S:
                missed_pings += 1
                if missed_pings >= PING_MISS_LIMIT:
                    print("[CommandStream] Missed ping threshold — reconnecting")
                    missed_pings = 0
                    last_ping_at = time.monotonic()

            if time.monotonic() - last_sync_at >= SYNC_HEARTBEAT_S:
                _run_sync_heartbeat(client)
                last_sync_at = time.monotonic()

        except KccPermanentFailure as exc:
            print(f"[CommandStream] Permanent SSE failure: {exc}")
            _stop_event.wait(30)
        except KccUnavailable as exc:
            print(f"[CommandStream] SSE unavailable, reconnecting: {exc}")
            _stop_event.wait(5)
        except Exception as exc:
            print(f"[CommandStream] Stream error: {exc}")
            _stop_event.wait(5)


def _run_loop() -> None:
    client = get_kcc_client()
    if client is None:
        return
    print("[CommandStream] Started")
    while not _stop_event.is_set():
        _consume_stream(client)
    print("[CommandStream] Stopped")


def start_command_stream() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if get_kcc_client() is None:
        print("[CommandStream] Skipped — KCC_API_URL/DEVICE_ID/DEVICE_TOKEN not set")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run_loop, name="command-stream", daemon=True)
    _thread.start()


def stop_command_stream() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
