"""Principal-bound replay reads and bounded actual ASGI publication for realtime only."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Send
from starlette.types import Scope as ASGIScope

from core.auth.dependencies import require_account
from core.auth.public import authenticated_session_ref, get_active_account
from core.auth.schemas import AccountRead, AccountSessionRef
from core.database import get_session
from core.realtime import (
    MAX_CURSOR_LENGTH,
    MAX_REPLAY_BATCH,
    ReplayCursor,
    ReplayRecord,
    ReplayState,
    current_head,
    parse_cursor,
)
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, WorkspaceContext

router = APIRouter(prefix="/api/v1/realtime", tags=["realtime"])
Session = Annotated[AsyncSession, Depends(get_session)]
AccountAdmission = Annotated[object, Depends(require_account)]
POLL_INTERVAL_SECONDS = 2
HEARTBEAT_INTERVAL_SECONDS = 15
DB_READ_TIMEOUT_SECONDS = 3
SEND_TIMEOUT_SECONDS = 2
CLEANUP_TIMEOUT_SECONDS = 2
MAX_STREAMS_PER_API_PROCESS = 32  # D3: gated on a T8 rerun (no pool exhaustion / idle-in-transaction growth)
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie, X-Workspace-ID"}


class SnapshotRead(BaseModel):
    """Current principal cursor and retention floor; no foreign sequence or identity metadata."""
    model_config = ConfigDict(frozen=True)

    cursor: str
    floor_sequence: str


class _RealtimeSendDenied(Exception):
    """Stop delegated response publication without exposing a protected denial body."""

    def __init__(self, failure: HTTPException) -> None:
        """Keep only the generic admission/unavailability error for a not-yet-started response."""
        super().__init__(failure.detail)
        self.failure = failure


def _denial_failure(exc: BaseException) -> HTTPException | None:
    """Recognize only send denial or a group composed exclusively of expected denial leaves.

    Cleanup/invalidation failure, cancellation or any unrelated leaf rejects the entire
    group so the original exception propagates after iterator/permit finalization. Causes
    and stale captured admission state never turn a disposal failure into normal completion.
    """
    if isinstance(exc, _RealtimeSendDenied):
        return exc.failure
    if isinstance(exc, BaseExceptionGroup):
        failures = [_denial_failure(child) for child in exc.exceptions]
        if failures and all(failure is not None for failure in failures):
            return failures[0]
    return None


async def _cleanup_session(session: AsyncSession) -> None:
    """Finish owned SQL rollback/close before permit release, shielding cancellation.

    Bound each cleanup phase to two seconds; failed rollback/close invalidates the connection
    instead of returning an open transaction. Invalidation failure propagates; no rollback
    task is left running in the background. Applies only to this finite realtime surface.
    """
    with anyio.CancelScope(shield=True):
        try:
            with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS):
                await session.rollback()
                await session.close()
        except BaseException:
            with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS):
                await session.invalidate()
            raise


class _RealtimeResponse(Response):
    """Guard actual snapshot/SSE ASGI sends with exact original admission, not generator checks.

    This owns one already-acquired stream permit if supplied. Every start/nonempty body
    locks auth/exact session/workspace under a fresh3s SQL budget, awaits that actual send
    for at most2s, then rolls back/closes before another send or wait. Broader W4 publication
    and W3 resource/member fanout are outside this private realtime-only wrapper.
    """

    def __init__(
        self, response: Response, *, request: Request, workspace: WorkspaceContext,
        access_fence: AccessFence, auth_session: AccountSessionRef,
        stream_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        """Capture original typed owner context/fence/session; assume ownership of supplied permit."""
        super().__init__(status_code=response.status_code, media_type=response.media_type)
        self.raw_headers = response.raw_headers
        self.response = response
        self.request = request
        self.workspace = workspace
        self.access_fence = access_fence
        self.auth_session = auth_session
        self.stream_semaphore = stream_semaphore

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        """Delegate actual sends under short locks; close on denial/uncertain send and release once.

        Before any start attempt a generic safe HTTP error may be returned. After a start
        attempt, never issue a second start. No protected failure event is sent after denial;
        an empty terminal body may close a known-started response. Wrapped streaming tasks
        and iterator are finished before the outer finally releases the transport permit.
        Only expected denial-only exceptions are handled; disposal/unrelated failures propagate.
        """
        start_attempted = False
        started = False
        finished = False
        send_uncertain = False
        factory: async_sessionmaker[AsyncSession] = self.request.app.state.session_factory

        async def guarded_send(message: Message) -> None:
            """Authorize the exact downstream start/body callback, with bounded SQL/send and cleanup."""
            nonlocal start_attempted, started, finished, send_uncertain
            if message["type"] not in {"http.response.start", "http.response.body"}:
                await send(message)
                return
            protected = message["type"] == "http.response.start" or bool(message.get("body", b""))
            session = None
            send_attempted = False
            try:
                if protected:
                    session = factory()
                    async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
                        await workspaces.lock_access_fence(
                            session, scope=self.workspace, expected=self.access_fence,
                            multi_workspace_enabled=self.request.app.state.settings.multi_workspace_enabled,
                            auth_sessions=(self.auth_session,),
                        )
                if message["type"] == "http.response.start":
                    start_attempted = True
                send_attempted = True
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    await send(message)
                if message["type"] == "http.response.start":
                    started = True
                elif not message.get("more_body", False):
                    finished = True
            except HTTPException as exc:
                raise _RealtimeSendDenied(exc) from exc
            except (TimeoutError, SQLAlchemyError) as exc:
                send_uncertain = send_attempted
                failure = HTTPException(status_code=503, detail="Realtime stream is temporarily unavailable")
                raise _RealtimeSendDenied(failure) from exc
            finally:
                if session is not None:
                    await _cleanup_session(session)

        try:
            try:
                await self.response(scope, receive, guarded_send)
            except Exception as exc:
                # A cleanup exception can replace the original denial, and task groups
                # can contain unrelated leaves. Classify the actual exception, not state
                # left behind by the earlier admission failure.
                denial = _denial_failure(exc)
                if denial is None:
                    raise
                if not start_attempted:
                    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                        # Use the installed HTTP error envelope rather than FastAPI's
                        # raw detail shape, retaining status/headers/redaction/request ID.
                        handler = self.request.app.exception_handlers[StarletteHTTPException]
                        error_response = await handler(self.request, denial)
                        error_response.headers.update(_PRIVATE_HEADERS)
                        await error_response(scope, receive, send)
                elif started and not finished and not send_uncertain:
                    try:
                        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                            await send({"type": "http.response.body", "body": b"", "more_body": False})
                    except (TimeoutError, OSError):
                        pass
        finally:
            try:
                iterator = getattr(self.response, "body_iterator", None)
                if iterator is not None and hasattr(iterator, "aclose"):
                    with anyio.CancelScope(shield=True):
                        with anyio.fail_after(CLEANUP_TIMEOUT_SECONDS):
                            await iterator.aclose()
            finally:
                if self.stream_semaphore is not None:
                    self.stream_semaphore.release()
                    self.stream_semaphore = None


def _parse_workspace_selection(value: str | None) -> UUID | None:
    """Parse query/header UUID with contractual400, before any visible membership lookup."""
    if value is None:
        return None
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid_workspace_id") from None


async def _select_realtime_workspace(
    session: AsyncSession, request: Request, workspace_id: str | None, workspace_header: str | None,
) -> WorkspaceContext:
    """Resolve genuine selected owner membership through public APIs and actual rollout gate.

    Query selection is required except proven bootstrap default; a supplied header must agree
    and never substitutes for an absent query selection. Malformed/missing/conflicting400,
    inactive401 and invisible404 precede any cursor disclosure.
    """
    account = getattr(request.state, "account", None)
    if not isinstance(account, AccountRead):
        raise HTTPException(status_code=401, detail="Authentication required")
    current = await get_active_account(session, account.id,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if current is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    selected = _parse_workspace_selection(workspace_id)
    header = _parse_workspace_selection(workspace_header)
    if selected is None:
        if current.id != 1:
            raise HTTPException(status_code=400, detail="workspace_required")
        selected = current.default_workspace_id
    if header is not None and header != selected:
        raise HTTPException(status_code=400, detail="workspace_selection_conflict")
    context = await workspaces.resolve_workspace_context(session, current.id, selected)
    if context is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return context


async def _prepare_realtime(
    session: AsyncSession, request: Request, workspace_id: str | None, workspace_header: str | None,
) -> tuple[WorkspaceContext, AccessFence, AccountSessionRef, ReplayState]:
    """Detach original admitted owner/session/head and release request SQL before response lifetime.

    Fresh-read preparation is bounded3s and grants no actual-send authority. Cleanup releases
    the dependency connection even on denial/cancellation so four streams can share the pool
    with their fresh publication sessions. Original fence is never upgraded after revocation.
    """
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
            auth_session = authenticated_session_ref(request)
            workspace = await _select_realtime_workspace(session, request, workspace_id, workspace_header)
            fence = await workspaces.read_access_fence(session, scope=workspace,
                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
            head = await current_head(session, scope=workspace, access_fence=fence,
                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
            return workspace, fence, auth_session, head
    except (TimeoutError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail="Realtime stream is temporarily unavailable") from exc
    finally:
        await _cleanup_session(session)


@router.get("/snapshot", response_model=SnapshotRead)
async def get_snapshot(
    session: Session, request: Request, auth_admission: AccountAdmission,
    workspace_id: Annotated[str | None, Query()] = None,
    workspace_header: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> Response:
    """Return existing snapshot wire fields only through the actual start/body publication gate."""
    workspace, fence, auth_session, head = await _prepare_realtime(session, request, workspace_id, workspace_header)
    snapshot = SnapshotRead(cursor=ReplayCursor(epoch=head.epoch, sequence=head.sequence).encode(),
                            floor_sequence=str(head.floor_sequence))
    return _RealtimeResponse(JSONResponse(snapshot.model_dump(mode="json"), headers=_PRIVATE_HEADERS),
        request=request, workspace=workspace, access_fence=fence, auth_session=auth_session)


def _resync(reason: str, cursor: str) -> str:
    """Format only a same-principal resnapshot cursor/reason; actual send still revalidates original fence."""
    payload = json.dumps({"reason": reason, "snapshot_cursor": cursor}, separators=(",", ":"), ensure_ascii=False)
    return f"event: resync_required\ndata: {payload}\n\n"


def _sse_record(record: ReplayRecord) -> str:
    """Detach one filtered persisted row using the exact compact Unicode serialization bound at append."""
    cursor = ReplayCursor(epoch=record.epoch, sequence=record.sequence).encode()
    payload = json.dumps(record.payload, separators=(",", ":"), ensure_ascii=False)
    return f"id: {cursor}\nevent: {record.event_type}\ndata: {payload}\n\n"


@dataclass(frozen=True, slots=True)
class _RealtimePage:
    """Bounded detached page/messages; never retains ORM or a SQL transaction across yields."""
    head: ReplayState
    messages: tuple[tuple[ReplayCursor, str], ...]
    reason: str | None


class _PermitResponse(StreamingResponse):
    """Streaming response that frees the stream permit if the generator never started (failed send, early error)."""

    def __init__(self, content: AsyncIterator[str], started: asyncio.Event, semaphore: asyncio.Semaphore, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._started = started
        self._semaphore = semaphore

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        try:
            # No listen_for_disconnect task group (Starlette spec < 2.4): its scope cancel would land inside the
            # body generator mid-DB-await and cancel the session rollback. Both generators poll
            # request.is_disconnected(), so they exit at a clean point; uvicorn's send is a no-op after disconnect.
            await self.stream_response(send)
        except OSError:
            raise ClientDisconnect() from None  # spec 2.4 servers raise on send once the client is gone
        finally:
            # Starlette never closes body_iterator; close it here so the generator's finally (permit
            # release, rollback) runs now, not at GC. Shielded + bounded for servers that cancel the task.
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                    await aclose()
            if not self._started.is_set():
                self._started.set()  # idempotent guard against double release
                self._semaphore.release()


async def _read_replay_page(
    request: Request, *, workspace: WorkspaceContext, fence: AccessFence,
    auth_session: AccountSessionRef, position: ReplayCursor,
) -> _RealtimePage:
    """Revalidate original exact session/fence and query principal/epoch before ORDER/LIMIT100.

    Release/close SQL before returning detached messages. Retention expiry/gaps cause same-
    stream resnapshot; admission denial terminates without protected error payload. No global
    cursor or member projection is available through this private owner stream.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    session = factory()
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT_SECONDS):
            await workspaces.lock_access_fence(session, scope=workspace, expected=fence,
                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled, auth_sessions=(auth_session,))
            head = await current_head(session, scope=workspace, access_fence=fence,
                multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
            reason = ("epoch_changed" if position.epoch != head.epoch
                      else "cursor_expired" if position.sequence < head.floor_sequence - 1 else None)
            if position.epoch == head.epoch and position.sequence > head.sequence:
                raise HTTPException(status_code=400, detail="Replay cursor is ahead of the current stream")
            records = [] if reason or getattr(workspace, "role", "owner") != "owner" else list((await session.scalars(select(ReplayRecord).where(
                ReplayRecord.workspace_id == workspace.workspace_id, ReplayRecord.user_id == workspace.user_id,
                ReplayRecord.epoch == head.epoch, ReplayRecord.sequence > position.sequence,
                ReplayRecord.sequence <= head.sequence,
            ).order_by(ReplayRecord.sequence).limit(MAX_REPLAY_BATCH))).all())
            expected = position.sequence + 1
            if not reason and (any(record.sequence != expected + index for index, record in enumerate(records))
                    or (not records and head.sequence > position.sequence)
                    or (records and len(records) < MAX_REPLAY_BATCH and records[-1].sequence != head.sequence)):
                reason = "replay_gap"
            # One chunk per page: the stream does one guarded (fence-locked) send for the whole batch.
            messages = (() if reason or not records else
                        ((ReplayCursor(epoch=records[-1].epoch, sequence=records[-1].sequence),
                          "".join(_sse_record(row) for row in records)),))
            return _RealtimePage(head=head, messages=messages, reason=reason)
    finally:
        await _cleanup_session(session)


@router.get("/events")
async def stream_events(
    session: Session, request: Request, auth_admission: AccountAdmission,
    workspace_id: Annotated[str | None, Query()] = None,
    workspace_header: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=MAX_CURSOR_LENGTH)] = None,
) -> Response:
    """Prepare owner replay, bound transport slots, and transfer permit to actual-send wrapper.

    Polls detach <=100 messages then release SQL; disconnect checks/yields/sleeps hold no
    connection. Original principal/session/fence applies throughout; revocation closes the
    stream rather than emitting new protected control content. Members await W3 fanout.
    """
    workspace, fence, auth_session, head = await _prepare_realtime(session, request, workspace_id, workspace_header)
    selected = last_event_id if last_event_id else cursor
    try:
        parsed = parse_cursor(selected) if selected else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Replay cursor is invalid") from exc
    initial = parsed or ReplayCursor(epoch=head.epoch, sequence=head.sequence)
    if initial.epoch == head.epoch and initial.sequence > head.sequence:
        raise HTTPException(status_code=400, detail="Replay cursor is ahead of the current stream")
    initial_reason = ("epoch_changed" if initial.epoch != head.epoch
                      else "cursor_expired" if initial.sequence < head.floor_sequence - 1 else None)


    async def body() -> AsyncIterator[str]:
        """Yield detached same-stream events/resync or15s heartbeat; no SQL transaction survives a yield."""
        position = initial
        if initial_reason:
            yield _resync(initial_reason, ReplayCursor(epoch=head.epoch, sequence=head.sequence).encode())
            return
        last_heartbeat = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            try:
                page = await _read_replay_page(request, workspace=workspace, fence=fence,
                                               auth_session=auth_session, position=position)
            except (HTTPException, TimeoutError, SQLAlchemyError):
                return
            if page.reason:
                yield _resync(page.reason, ReplayCursor(epoch=page.head.epoch, sequence=page.head.sequence).encode())
                return
            if page.messages:
                for event_position, message in page.messages:
                    if await request.is_disconnected():
                        return
                    yield message
                    # A downstream send failure closes/cancels this iterator. Advancing
                    # locally after yield therefore cannot resume a failed connection.
                    position = event_position
                last_heartbeat = asyncio.get_running_loop().time()
                continue
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    semaphore: asyncio.Semaphore = request.app.state.realtime_connections
    acquired = False
    try:
        try:
            # Await directly in this task so cancellation cannot strand an acquired
            # child-task permit between wait_for completion and ownership transfer.
            async with asyncio.timeout(0.01):
                await semaphore.acquire()
                acquired = True
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail="Realtime connection limit reached") from exc
        response = _RealtimeResponse(StreamingResponse(body(), media_type="text/event-stream", headers={
            **_PRIVATE_HEADERS, "Cache-Control": "private, no-store, no-transform",
            "X-Accel-Buffering": "no", "Connection": "keep-alive",
        }), request=request, workspace=workspace, access_fence=fence, auth_session=auth_session,
            stream_semaphore=semaphore)
        acquired = False
        return response
    finally:
        if acquired:
            semaphore.release()
