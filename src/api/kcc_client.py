"""Typed HTTP client for kcc-app device-facing REST (trpc-openapi projection)."""

from __future__ import annotations

import json
from typing import Any, Iterator, Literal, Optional, Union

import httpx
import sseclient
from pydantic import BaseModel

from src.config import DEVICE_ID, DEVICE_TOKEN, KCC_API_URL
from src.schema import CadetAttendance, Person
from src.utils import ist_timestamp, python_string_to_timestamp

PERMANENT_FAILURE = "PERMANENT_FAILURE"


class KccUnavailable(Exception):
    """Retryable network or 5xx failure — leave syncedAt NULL for sweeper."""


class KccPermanentFailure(Exception):
    """4xx validation/auth failure — dead-letter the row."""


class AttendanceRecordIn(BaseModel):
    deviceId: str
    idempotencyKey: str
    cadetId: str
    sessionId: str
    attendanceTimestamp: str
    capturedAt: str
    matchScore: Optional[float] = None


class AttendanceRecordOut(BaseModel):
    accepted: Literal[True]
    cadetId: str
    sessionId: str
    attendanceLogId: str
    syncedAt: str


class EnrollmentConfirmIn(BaseModel):
    deviceId: str
    idempotencyKey: str
    cadetId: str
    hubId: str
    enrolledAt: str
    pictureFileName: Optional[str] = None


class EnrollmentConfirmOut(BaseModel):
    accepted: Literal[True]
    cadetId: str
    enrollmentId: str
    syncedAt: str


class CommandAckIn(BaseModel):
    deviceId: str
    commandId: str
    status: Literal["applied", "failed", "refused"]
    appliedAt: str
    detail: Optional[str] = None


class CommandAckOut(BaseModel):
    acknowledged: Literal[True]
    commandId: str
    status: Literal["applied", "failed", "refused"]


class DeleteEmbeddingCommand(BaseModel):
    kind: Literal["delete-embedding"]
    commandId: str
    cadetId: str
    hubId: str
    issuedAt: str


class UnenrollCommand(BaseModel):
    kind: Literal["unenroll"]
    commandId: str
    cadetId: str
    issuedAt: str


class RenamePersonCommand(BaseModel):
    kind: Literal["rename-person"]
    commandId: str
    cadetId: str
    preferredName: str
    issuedAt: str


class StartSessionCommand(BaseModel):
    kind: Literal["start-session"]
    commandId: str
    sessionId: str
    name: str
    startTimestamp: str
    plannedEndTimestamp: str
    plannedDurationInMinutes: int
    issuedAt: str


class EndSessionCommand(BaseModel):
    kind: Literal["end-session"]
    commandId: str
    sessionId: str
    actualEndTimestamp: str
    issuedAt: str


class PingCommand(BaseModel):
    kind: Literal["ping"]
    commandId: str
    issuedAt: str


CommandEnvelope = Union[
    DeleteEmbeddingCommand,
    UnenrollCommand,
    RenamePersonCommand,
    StartSessionCommand,
    EndSessionCommand,
    PingCommand,
]


class CommandsSyncRequest(BaseModel):
    deviceId: str
    localCadetIds: list[str]
    localHubIds: list[str]


class CommandsSyncDeleteItem(BaseModel):
    cadetId: str
    hubId: str


class CommandsSyncEnrollItem(BaseModel):
    cadetId: str


class CommandsSyncResponse(BaseModel):
    expectedCadetIds: list[str]
    deleteThese: list[CommandsSyncDeleteItem]
    enrollThese: list[CommandsSyncEnrollItem]


def _parse_command(data: dict[str, Any]) -> CommandEnvelope:
    kind = data.get("kind")
    parsers: dict[str, type[BaseModel]] = {
        "delete-embedding": DeleteEmbeddingCommand,
        "unenroll": UnenrollCommand,
        "rename-person": RenamePersonCommand,
        "start-session": StartSessionCommand,
        "end-session": EndSessionCommand,
        "ping": PingCommand,
    }
    model = parsers.get(kind or "")
    if model is None:
        raise ValueError(f"Unknown command kind: {kind!r}")
    return model.model_validate(data)  # type: ignore[return-value]


class KccDeviceClient:
    """Typed client for kcc-app device REST endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        device_id: str,
        token: str,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.token = token
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post_json(self, path: str, body: BaseModel) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.post(
                url,
                headers=self._headers(),
                json=body.model_dump(exclude_none=True),
                timeout=self.timeout_s,
            )
        except httpx.RequestError as exc:
            raise KccUnavailable(str(exc)) from exc

        if resp.status_code >= 500:
            raise KccUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise KccPermanentFailure(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise KccUnavailable(f"Invalid JSON response: {exc}") from exc

    def record_attendance(
        self,
        row: CadetAttendance,
        *,
        captured_at: Optional[str] = None,
        match_score: Optional[float] = None,
    ) -> AttendanceRecordOut:
        attendance_ts = python_string_to_timestamp(row.attendanceTimeStamp) or ist_timestamp()
        payload = AttendanceRecordIn(
            deviceId=self.device_id,
            idempotencyKey=f"{self.device_id}:{row.id}",
            cadetId=row.personId,
            sessionId=row.sessionId,
            attendanceTimestamp=attendance_ts,
            capturedAt=captured_at or attendance_ts,
            matchScore=match_score,
        )
        data = self._post_json("/openapi/device.attendance.record", payload)
        return AttendanceRecordOut.model_validate(data)

    def confirm_enrollment(
        self,
        person: Person,
        hub_id: str,
        *,
        enrolled_at: Optional[str] = None,
    ) -> EnrollmentConfirmOut:
        payload = EnrollmentConfirmIn(
            deviceId=self.device_id,
            idempotencyKey=f"{self.device_id}:enroll:{person.uniqueId}",
            cadetId=person.uniqueId,
            hubId=str(hub_id),
            enrolledAt=enrolled_at or ist_timestamp(),
            pictureFileName=person.pictureFileName,
        )
        data = self._post_json("/openapi/device.enrollment.confirm", payload)
        return EnrollmentConfirmOut.model_validate(data)

    def ack_command(self, ack: CommandAckIn) -> CommandAckOut:
        data = self._post_json("/openapi/device.commands.ack", ack)
        return CommandAckOut.model_validate(data)

    def sync_commands(
        self,
        local_cadet_ids: list[str],
        local_hub_ids: list[str],
    ) -> CommandsSyncResponse:
        payload = CommandsSyncRequest(
            deviceId=self.device_id,
            localCadetIds=local_cadet_ids,
            localHubIds=local_hub_ids,
        )
        data = self._post_json("/openapi/device.commands.sync", payload)
        return CommandsSyncResponse.model_validate(data)

    def open_command_stream(
        self,
        last_event_id: int | None,
    ) -> Iterator[tuple[int | None, str, CommandEnvelope | None]]:
        """Yield (event_id, event_type, parsed_command) from the SSE stream."""
        params: dict[str, str] = {"deviceId": self.device_id}
        if last_event_id is not None:
            params["lastEventId"] = str(last_event_id)

        url = f"{self.base_url}/openapi/device/commands/stream"
        # The stream never ends on its own, so it must be consumed
        # incrementally (httpx.stream); a plain httpx.get would block forever
        # buffering the body. The server pings every 15s — a read timeout well
        # above that turns a silently dead connection into a reconnect.
        timeout = httpx.Timeout(
            connect=self.timeout_s,
            read=90.0,
            write=self.timeout_s,
            pool=self.timeout_s,
        )
        try:
            with httpx.stream(
                "GET",
                url,
                headers=self._headers(),
                params=params,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    if resp.status_code >= 500:
                        raise KccUnavailable(f"SSE HTTP {resp.status_code}")
                    raise KccPermanentFailure(
                        f"SSE HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                # sseclient expects a binary chunk iterator.
                yield from self._iter_stream_events(resp.iter_bytes())
        except httpx.HTTPError as exc:
            raise KccUnavailable(str(exc)) from exc

    def _iter_stream_events(
        self,
        byte_iter: Iterator[bytes],
    ) -> Iterator[tuple[int | None, str, CommandEnvelope | None]]:
        client = sseclient.SSEClient(byte_iter)
        for event in client.events():
            event_id: int | None = None
            if event.id:
                try:
                    event_id = int(event.id)
                except ValueError:
                    pass

            event_type = event.event or "message"

            if event_type == "resync":
                yield event_id, event_type, None
                continue

            if event_type in ("ping", "message") and event.data:
                try:
                    data = json.loads(event.data)
                except json.JSONDecodeError:
                    continue
                if data.get("kind") == "ping":
                    yield event_id, "ping", PingCommand.model_validate(data)
                    continue

            if event_type == "command" and event.data:
                try:
                    data = json.loads(event.data)
                    yield event_id, event_type, _parse_command(data)
                except (json.JSONDecodeError, ValueError) as exc:
                    print(f"[CommandStream] Skipping malformed command: {exc}")
                continue

            if event.data:
                try:
                    data = json.loads(event.data)
                    if "kind" in data:
                        yield event_id, event_type, _parse_command(data)
                except (json.JSONDecodeError, ValueError):
                    pass


_client: KccDeviceClient | None = None


def get_kcc_client() -> KccDeviceClient | None:
    """Return a shared client, or None when device credentials are not configured."""
    global _client
    if not KCC_API_URL or not DEVICE_ID or not DEVICE_TOKEN:
        return None
    if _client is None:
        _client = KccDeviceClient(
            base_url=KCC_API_URL,
            device_id=DEVICE_ID,
            token=DEVICE_TOKEN,
        )
    return _client
