"""Background sweeper draining unsynced CadetAttendance and Person rows."""

from __future__ import annotations

import threading

from src.api.kcc_client import (
    KccPermanentFailure,
    KccUnavailable,
    PERMANENT_FAILURE,
    get_kcc_client,
)
from src.schema import CadetAttendance, FaceIdentityMap, Person, db

BATCH_SIZE = 50
INITIAL_INTERVAL_S = 2.0
MAX_BACKOFF_S = 300.0

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_backoff_s = INITIAL_INTERVAL_S


def _dead_letter(row, message: str) -> None:
    row.syncedAt = PERMANENT_FAILURE
    row.error = message[:500]
    row.save()


def _sync_attendance_row(client, row: CadetAttendance) -> None:
    resp = client.record_attendance(row)
    row.syncedAt = resp.syncedAt
    row.error = None
    row.save()


def _sync_person_row(client, row: Person) -> None:
    mapping = FaceIdentityMap.get_or_none(FaceIdentityMap.personId == row.uniqueId)
    if not mapping:
        _dead_letter(row, "missing FaceIdentityMap for enrollment confirm")
        return
    resp = client.confirm_enrollment(row, str(mapping.hubId))
    row.syncedAt = resp.syncedAt
    row.error = None
    row.save()


def _sweep_once() -> bool:
    """Run one sweep pass. Returns True if any row was processed."""
    client = get_kcc_client()
    if client is None:
        return False

    processed = False

    if db.is_closed():
        db.connect(reuse_if_open=True)

    attendance_rows = (
        CadetAttendance.select()
        .where(CadetAttendance.syncedAt.is_null())
        .order_by(CadetAttendance.id.asc())
        .limit(BATCH_SIZE)
    )
    for row in attendance_rows:
        processed = True
        try:
            _sync_attendance_row(client, row)
            print(f"[SyncSweeper] Attendance synced rowid={row.id}")
        except KccUnavailable as exc:
            print(f"[SyncSweeper] KCC unavailable (attendance): {exc}")
            raise
        except KccPermanentFailure as exc:
            print(f"[SyncSweeper] Dead-letter attendance rowid={row.id}: {exc}")
            _dead_letter(row, str(exc))

    person_rows = (
        Person.select()
        .where(Person.syncedAt.is_null())
        .order_by(Person.uniqueId.asc())
        .limit(BATCH_SIZE)
    )
    for row in person_rows:
        processed = True
        try:
            _sync_person_row(client, row)
            print(f"[SyncSweeper] Enrollment confirmed person={row.uniqueId}")
        except KccUnavailable as exc:
            print(f"[SyncSweeper] KCC unavailable (enrollment): {exc}")
            raise
        except KccPermanentFailure as exc:
            print(f"[SyncSweeper] Dead-letter person={row.uniqueId}: {exc}")
            _dead_letter(row, str(exc))

    return processed


def _run_loop() -> None:
    global _backoff_s
    print("[SyncSweeper] Started")
    while not _stop_event.is_set():
        try:
            _sweep_once()
            _backoff_s = INITIAL_INTERVAL_S
            _stop_event.wait(INITIAL_INTERVAL_S)
        except KccUnavailable:
            print(f"[SyncSweeper] Backing off {_backoff_s:.0f}s")
            _stop_event.wait(_backoff_s)
            _backoff_s = min(_backoff_s * 2, MAX_BACKOFF_S)
        except Exception as exc:
            print(f"[SyncSweeper] Unexpected error: {exc}")
            _stop_event.wait(_backoff_s)
            _backoff_s = min(_backoff_s * 2, MAX_BACKOFF_S)
    print("[SyncSweeper] Stopped")


def start_sync_sweeper() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if get_kcc_client() is None:
        print("[SyncSweeper] Skipped — KCC_API_URL/DEVICE_ID/DEVICE_TOKEN not set")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run_loop, name="sync-sweeper", daemon=True)
    _thread.start()


def stop_sync_sweeper() -> None:
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
