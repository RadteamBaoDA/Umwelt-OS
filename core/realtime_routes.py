import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

from core.auth.dependencies import SESSION_COOKIE
from core.auth.models import AuthSession
from core.database import get_session
from core.realtime import (
    MAX_REPLAY_BATCH,
    ReplayCursor,
    ReplayRecord,
    current_head,
    parse_cursor,
)

router = APIRouter(prefix="/api/v1/realtime", tags=["realtime"])
Session = Annotated[AsyncSession, Depends(get_session)]
POLL_INTERVAL_SECONDS = 2
HEARTBEAT_INTERVAL_SECONDS = 15
DB_READ_TIMEOUT_SECONDS = 3
MAX_STREAMS_PER_API_PROCESS = 32
_TOKEN_CURSOR_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotRead(BaseModel):
    """Immutable response containing the current replay cursor and earliest retained sequence."""
    model_config = ConfigDict(frozen=True)

    cursor: str
    floor_sequence: str


def _token_hash(request: Request) -> str | None:
    """Hash the session cookie token for lookup without returning or storing the raw token."""
    token = request.cookies.get(SESSION_COOKIE)
    return hashlib.sha256(token.encode()).hexdigest() if token else None


async def _token_is_current(session: AsyncSession, token_hash: str | None) -> bool:
    """Return whether the hashed session token exists and is unexpired, using the caller's session."""
    if token_hash is None or not _TOKEN_CURSOR_RE.fullmatch(token_hash):
        return False
    return await session.scalar(
        select(AuthSession.token_hash).where(
            AuthSession.token_hash == token_hash,
            AuthSession.expires_at > datetime.now(UTC),
        ).limit(1)
    ) is not None


async def _session_is_current(request: Request) -> bool | None:
    """Check session existence and expiry within a bounded database read; return None when the read times out."""
    factory = request.app.state.session_factory
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
            async with factory() as session:
                return await _token_is_current(session, _token_hash(request))
    except TimeoutError:
        return None


@router.get("/snapshot", response_model=SnapshotRead)
async def get_snapshot(session: Session, request: Request, response: Response) -> SnapshotRead:
    """Return the authenticated replay snapshot with private cache headers; report unavailable storage as 503."""
    current = await _session_is_current(request)
    if current is None:
        raise HTTPException(status_code=503, detail="Realtime snapshot is temporarily unavailable")
    if not current:
        raise HTTPException(status_code=401, detail="Authentication required")
    head = await current_head(session)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return SnapshotRead(
        cursor=ReplayCursor(epoch=head.epoch, sequence=head.sequence).encode(),
        floor_sequence=str(head.floor_sequence),
    )


def _resync(reason: str, cursor: str) -> str:
    """Format an SSE resync instruction with the reason and current snapshot cursor."""
    payload = json.dumps({"reason": reason, "snapshot_cursor": cursor}, separators=(",", ":"))
    return f"event: resync_required\ndata: {payload}\n\n"


def _sse_record(record: ReplayRecord) -> str:
    """Serialize one persisted replay row as an SSE event with its resumable cursor."""
    cursor = ReplayCursor(epoch=record.epoch, sequence=record.sequence).encode()
    payload = json.dumps(record.payload, separators=(",", ":"), ensure_ascii=False)
    return f"id: {cursor}\nevent: {record.event_type}\ndata: {payload}\n\n"


class _PermitResponse(StreamingResponse):
    """Streaming response that frees the stream permit if the generator never started (failed send, early error)."""

    def __init__(self, content: AsyncIterator[str], started: asyncio.Event, semaphore: asyncio.Semaphore, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._started = started
        self._semaphore = semaphore

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette never closes body_iterator (e.g. a disconnect cancels the send task mid-stream);
            # close it here so the generator's finally (permit release, rollback) runs now, not at GC.
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()
            if not self._started.is_set():
                self._started.set()  # idempotent guard against double release
                self._semaphore.release()


@router.get("/events")
async def stream_events(
    request: Request,
    cursor: Annotated[str | None, Query(max_length=60)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=60)] = None,
) -> StreamingResponse:
    """Authenticate and stream ordered replay events, resync on epoch/retention/gap changes, and cap concurrent streams."""
    current = await _session_is_current(request)
    if current is None:
        raise HTTPException(status_code=503, detail="Realtime stream is temporarily unavailable")
    if not current:
        raise HTTPException(status_code=401, detail="Authentication required")
    selected = last_event_id if last_event_id else cursor
    parsed: ReplayCursor | None = None
    if selected:
        try:
            parsed = parse_cursor(selected)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Replay cursor is invalid") from exc

    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
            async with factory() as session:
                head = await current_head(session)
                initial_cursor = parsed or ReplayCursor(epoch=head.epoch, sequence=head.sequence)
                if parsed is not None and parsed.sequence > head.sequence and parsed.epoch == head.epoch:
                    raise HTTPException(status_code=400, detail="Replay cursor is ahead of the current stream")
                initial_reason = (
                    "epoch_changed" if initial_cursor.epoch != head.epoch
                    else "cursor_expired" if initial_cursor.sequence < head.floor_sequence - 1
                    else None
                )
                latest_cursor = ReplayCursor(epoch=head.epoch, sequence=head.sequence).encode()
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Realtime stream is temporarily unavailable") from exc

    semaphore: asyncio.Semaphore = request.app.state.realtime_connections
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.01)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Realtime connection limit reached") from exc

    started = asyncio.Event()

    async def body() -> AsyncIterator[str]:
        """Poll replay state, recheck session validity, emit events or heartbeats, and release the stream permit on every exit."""
        nonlocal initial_reason, latest_cursor
        started.set()
        try:
            position = initial_cursor
            if initial_reason is not None:
                yield _resync(initial_reason, latest_cursor)
                return
            last_heartbeat = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                try:
                    async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
                        async with factory() as session:
                            head = await current_head(session)
                            stale_reason = (
                                "epoch_changed" if position.epoch != head.epoch
                                else "cursor_expired" if position.sequence < head.floor_sequence - 1
                                else None
                            )
                            records = [] if stale_reason else list((await session.scalars(
                                select(ReplayRecord)
                                .where(
                                    ReplayRecord.epoch == head.epoch,
                                    ReplayRecord.sequence > position.sequence,
                                    ReplayRecord.sequence <= head.sequence,
                                )
                                .order_by(ReplayRecord.sequence)
                                .limit(MAX_REPLAY_BATCH)
                            )).all())
                            latest_cursor = ReplayCursor(epoch=head.epoch, sequence=head.sequence).encode()
                            # Same session/snapshot as the read; still before any write below.
                            current = await _token_is_current(session, _token_hash(request))
                except TimeoutError:
                    yield "event: connection_unavailable\ndata: {}\n\n"
                    return
                if not current:
                    yield "event: auth_expired\ndata: {}\n\n"
                    return
                if stale_reason is not None:
                    yield _resync(stale_reason, latest_cursor)
                    return
                expected = position.sequence + 1
                if any(record.sequence != expected + index for index, record in enumerate(records)):
                    yield _resync("replay_gap", latest_cursor)
                    return
                if not records and head.sequence > position.sequence:
                    yield _resync("replay_gap", latest_cursor)
                    return
                if records and len(records) < MAX_REPLAY_BATCH and records[-1].sequence != head.sequence:
                    yield _resync("replay_gap", latest_cursor)
                    return
                if records:
                    # One write for the whole batch (records are invalidation pointers).
                    if await request.is_disconnected():
                        return
                    yield "".join(_sse_record(record) for record in records)
                    position = ReplayCursor(epoch=records[-1].epoch, sequence=records[-1].sequence)
                    last_heartbeat = asyncio.get_running_loop().time()
                    continue
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        finally:
            semaphore.release()

    return _PermitResponse(
        body(),
        started,
        semaphore,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
