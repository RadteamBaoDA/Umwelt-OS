"""Owner-authenticated HTTP routes for chat conversations, messages, SSE streaming, and run cancellation."""

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.dependencies import SESSION_COOKIE, require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.chat.models import Conversation, Message, ResponseRun, StreamEvent
from modules.chat import public as chat_public
from modules.chat.schemas import (
    CancelResponse,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    MessageRead,
    SendMessageRequest,
    SendMessageResponse,
)
from modules.chat.stream import format_sse_event, make_event_id, parse_event_id
from modules.chat.worker import CANCEL_KEY_PREFIX, run_response_generation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]

_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
DB_READ_TIMEOUT = 3.0
STREAM_POLL_INTERVAL = 0.1
HEARTBEAT_INTERVAL = 15.0


def _token_hash(request: Request) -> str | None:
    """Compute the SHA-256 hash of the session cookie for safe auth lookup.

    Args:
        request: FastAPI HTTP request.

    Returns:
        Hex-encoded SHA-256 token digest, or None if the cookie is missing.
    """
    token = request.cookies.get(SESSION_COOKIE)
    return hashlib.sha256(token.encode()).hexdigest() if token else None


async def _session_is_current(request: Request) -> bool | None:
    """Verify session validity against the database within a bounded timeout.

    Args:
        request: FastAPI HTTP request containing app state session factory.

    Returns:
        True if the session exists and has not expired; False if invalid or expired;
        None if database read timed out.
    """
    th = _token_hash(request)
    if th is None or not _TOKEN_RE.fullmatch(th):
        return False
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT):
            async with factory() as session:
                found = await session.scalar(
                    select(AuthSession.token_hash).where(
                        AuthSession.token_hash == th,
                        AuthSession.expires_at > datetime.now(UTC),
                    ).limit(1)
                )
                return found is not None
    except TimeoutError:
        return None


@router.get("/api/v1/conversations", response_model=list[ConversationRead])
async def list_conversations(
    session: Session,
    _owner: OwnerRead,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    archived: bool = False,
) -> list[ConversationRead]:
    """List persistent owner conversations ordered by most recent update.

    Args:
        session: Active asynchronous database session.
        _owner: Authenticated owner session dependency.
        response: HTTP response object to attach cache prevention headers.
        limit: Maximum number of conversations to return (1-100).
        offset: Offset for pagination.
        archived: Filter to archived or active conversations.

    Returns:
        List of ConversationRead schemas.
    """
    response.headers["Cache-Control"] = "private, no-store"
    rows = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.archived == archived)
            .order_by(desc(Conversation.pinned), desc(Conversation.updated_at))
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return [
        ConversationRead(
            id=row.id,
            title=row.title,
            context_kind=row.context_kind,
            context_resource_id=row.context_resource_id,
            pinned=row.pinned,
            archived=row.archived,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )
        for row in rows
    ]


@router.get("/api/v1/conversations/{conversation_id}/agent-runs/{run_id}/activity", response_model=chat_public.AgentActivityRead)
async def read_agent_activity(
    conversation_id: UUID, run_id: UUID, session: Session, owner: OwnerRead,
) -> chat_public.AgentActivityRead:
    """Read bounded agent activity through its owner-checked chat conversation link."""
    return await chat_public.get_agent_activity(
        session, conversation_id, run_id, owner.owner_id, owner.token_hash,
    )


@router.post("/api/v1/conversations", response_model=ConversationRead, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    session: Session,
    _owner: OwnerWrite,
) -> ConversationRead:
    """Create a new chat conversation with optional context linking.

    Args:
        payload: Conversation creation parameters.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Returns:
        Newly created ConversationRead schema.
    """
    title = (payload.title or "").strip() or "New conversation"
    conv = Conversation(
        title=title,
        context_kind=payload.context_kind,
        context_resource_id=payload.context_resource_id,
        metadata_json=payload.metadata,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)

    return ConversationRead(
        id=conv.id,
        title=conv.title,
        context_kind=conv.context_kind,
        context_resource_id=conv.context_resource_id,
        pinned=conv.pinned,
        archived=conv.archived,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        metadata=conv.metadata_json or {},
    )


@router.get("/api/v1/conversations/{conversation_id}", response_model=ConversationDetailRead)
async def get_conversation(
    conversation_id: UUID,
    session: Session,
    _owner: OwnerRead,
    response: Response,
) -> ConversationDetailRead:
    """Retrieve full conversation details including chronologically ordered messages.

    Args:
        conversation_id: UUID of the target conversation.
        session: Active asynchronous database session.
        _owner: Authenticated owner session dependency.
        response: HTTP response object for no-store header attachment.

    Returns:
        ConversationDetailRead schema containing conversation metadata and messages.

    Raises:
        HTTPException: 404 if conversation is not found.
    """
    response.headers["Cache-Control"] = "private, no-store"
    conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages_rows = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
    ).all()

    messages_list = [
        MessageRead(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            client_request_id=m.client_request_id,
            model_identity=m.model_identity,
            citations=m.citations or [],
            response_id=m.response_id,
            created_at=m.created_at,
        )
        for m in messages_rows
    ]

    return ConversationDetailRead(
        id=conv.id,
        title=conv.title,
        context_kind=conv.context_kind,
        context_resource_id=conv.context_resource_id,
        pinned=conv.pinned,
        archived=conv.archived,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        metadata=conv.metadata_json or {},
        messages=messages_list,
    )


@router.patch("/api/v1/conversations/{conversation_id}", response_model=ConversationRead)
async def patch_conversation(
    conversation_id: UUID,
    payload: ConversationPatch,
    session: Session,
    _owner: OwnerWrite,
) -> ConversationRead:
    """Update conversation title, pinned status, archived state, or metadata.

    Args:
        conversation_id: UUID of the conversation to modify.
        payload: Update fields.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Returns:
        Updated ConversationRead schema.

    Raises:
        HTTPException: 404 if conversation does not exist.
    """
    conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if payload.title is not None:
        conv.title = payload.title.strip() or "New conversation"
    if payload.pinned is not None:
        conv.pinned = payload.pinned
    if payload.archived is not None:
        conv.archived = payload.archived
    if payload.metadata is not None:
        current_meta = dict(conv.metadata_json or {})
        current_meta.update(payload.metadata)
        conv.metadata_json = current_meta

    conv.updated_at = func.now()  # type: ignore[assignment]
    await session.commit()
    await session.refresh(conv)

    return ConversationRead(
        id=conv.id,
        title=conv.title,
        context_kind=conv.context_kind,
        context_resource_id=conv.context_resource_id,
        pinned=conv.pinned,
        archived=conv.archived,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        metadata=conv.metadata_json or {},
    )


@router.delete("/api/v1/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    session: Session,
    _owner: OwnerWrite,
) -> None:
    """Delete conversation content and redact linked agent payloads while preserving effect tombstones.

    Args:
        conversation_id: UUID of the conversation to delete.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Side effects:
        Cancels linked agent runs and purges their prompts, checkpoint payloads, approval arguments,
        and tool-call arguments before the Chat-owned activity link cascades away. A possibly sent
        effect stays in requires_review so deletion cannot make its action replayable.

    Raises:
        HTTPException: 404 if conversation does not exist.
    """
    # Lock the Chat parent before agent runs and approvals; take any future upstream domain
    # lifecycle locks before this row so the cross-module deletion order remains acyclic.
    conv = await session.scalar(select(Conversation).where(
        Conversation.id == conversation_id,
    ).with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    from modules.agents.public import purge_conversation_actions

    await purge_conversation_actions(session, conversation_id, _owner.owner_id)
    await session.delete(conv)
    await session.commit()


@router.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=202,
)
async def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    session: Session,
    _owner: OwnerWrite,
) -> SendMessageResponse:
    """Accept a user message, enforce client_request_id idempotency, and dispatch response generation.

    Persists both the user message and a pending ResponseRun in the database before dispatching
    background worker processing. Repeated submissions with the same client_request_id return
    the existing response run without duplicated generation.

    Args:
        conversation_id: UUID of the parent conversation.
        payload: SendMessageRequest containing message content, optional client request ID and context.
        request: FastAPI request providing access to background task scheduling and Redis.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Returns:
        SendMessageResponse with message_id, response_id, and pending/active status.

    Raises:
        HTTPException: 404 if conversation is not found.
    """
    conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Idempotency check with client_request_id
    if payload.client_request_id:
        existing_run = await session.scalar(
            select(ResponseRun).where(
                ResponseRun.conversation_id == conversation_id,
                ResponseRun.client_request_id == payload.client_request_id,
            )
        )
        if existing_run is not None:
            return SendMessageResponse(
                message_id=existing_run.user_message_id,
                response_id=existing_run.id,
                status=existing_run.status,
            )

    # Persist user message and pending response run before dispatch
    user_msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=payload.content.strip(),
        client_request_id=payload.client_request_id,
    )
    session.add(user_msg)
    await session.flush()

    response_run = ResponseRun(
        conversation_id=conversation_id,
        user_message_id=user_msg.id,
        client_request_id=payload.client_request_id,
        status="pending",
        retrieval_context=payload.context or {},
    )
    session.add(response_run)

    conv.updated_at = func.now()  # type: ignore[assignment]
    await session.commit()
    await session.refresh(response_run)

    # Dispatch to ARQ and local asyncio background task
    redis: Redis = request.app.state.redis
    if hasattr(redis, "enqueue_job"):
        try:
            await redis.enqueue_job(
                "process_chat_response",
                str(response_run.id),
                _job_id=f"chat-response:{response_run.id}",
            )
        except Exception as exc:
            logger.warning("Failed to enqueue ARQ job: %s", exc)

    # Also schedule local background execution to ensure prompt processing
    asyncio.create_task(
        run_response_generation(
            response_id=response_run.id,
            session_factory=request.app.state.session_factory,
            settings=request.app.state.settings,
            redis=redis,
        )
    )

    return SendMessageResponse(
        message_id=user_msg.id,
        response_id=response_run.id,
        status="pending",
    )


@router.get("/api/v1/responses/{response_id}/events")
async def get_response_events(
    response_id: UUID,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    cursor: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """Stream response generation events using Server-Sent Events (SSE) with Last-Event-ID resume support.

    Replays stored stream events from the sequence requested by Last-Event-ID, then streams live
    generated deltas. Continuously validates authentication throughout connection lifetime;
    terminates immediately with 'auth_expired' if session expires or is revoked.

    Args:
        response_id: UUID of the ResponseRun to stream.
        request: FastAPI HTTP request providing session verification and cancellation detection.
        last_event_id: Header containing the last received SSE event ID for resumption.
        cursor: Query parameter fallback for Last-Event-ID.

    Returns:
        StreamingResponse with text/event-stream media type.

    Raises:
        HTTPException: 401 if unauthenticated, 404 if run not found.
    """
    initial_auth = await _session_is_current(request)
    if initial_auth is None:
        raise HTTPException(status_code=503, detail="Session verification temporarily unavailable")
    if not initial_auth:
        raise HTTPException(status_code=401, detail="Authentication required")

    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    # Verify ResponseRun exists
    async with factory() as session:
        run = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Response run not found")

    selected_cursor = last_event_id or cursor
    start_seq = 1
    if selected_cursor:
        try:
            _, parsed_seq = parse_event_id(selected_cursor)
            start_seq = parsed_seq + 1
        except ValueError:
            start_seq = 1

    async def sse_event_stream() -> Any:
        """Generator yielding formatted SSE chunks, polling new rows and enforcing session freshness."""
        current_seq = start_seq - 1
        last_heartbeat = asyncio.get_running_loop().time()

        while not await request.is_disconnected():
            # Refresh auth periodically; no tokens or response emitted after permission revoked
            auth_ok = await _session_is_current(request)
            if auth_ok is None:
                yield format_sse_event("status", {"status": "unavailable"})
                return
            if not auth_ok:
                yield format_sse_event("status", {"status": "auth_expired"})
                return

            # Read new stream events from DB
            try:
                async with factory() as session:
                    events = (
                        await session.scalars(
                            select(StreamEvent)
                            .where(
                                StreamEvent.response_id == response_id,
                                StreamEvent.seq > current_seq,
                            )
                            .order_by(StreamEvent.seq.asc())
                            .limit(100)
                        )
                    ).all()

                    current_run = await session.scalar(
                        select(ResponseRun).where(ResponseRun.id == response_id)
                    )
            except Exception as exc:
                logger.warning("Error querying stream events: %s", exc)
                events = []
                current_run = None

            if events:
                for ev in events:
                    if await request.is_disconnected():
                        return
                    yield format_sse_event(
                        event=ev.event_type,
                        data=ev.data,
                        event_id=ev.event_id,
                    )
                    current_seq = ev.seq
                last_heartbeat = asyncio.get_running_loop().time()

            # If the run has finished and we emitted all events up to completion/cancellation
            if current_run is not None and current_run.status in ("completed", "cancelled", "failed"):
                if not events:
                    # Stream complete
                    return

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            await asyncio.sleep(STREAM_POLL_INTERVAL)

    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/v1/responses/{response_id}/cancel", response_model=CancelResponse)
async def cancel_response(
    response_id: UUID,
    request: Request,
    session: Session,
    _owner: OwnerWrite,
) -> CancelResponse:
    """Request immediate cancellation of an active response run.

    Marks the ResponseRun status as cancelled, writes the cancellation signal to Redis,
    and appends a terminal status event.

    Args:
        response_id: UUID of the ResponseRun to cancel.
        request: FastAPI HTTP request providing Redis access.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Returns:
        CancelResponse schema with updated status.

    Raises:
        HTTPException: 404 if response run does not exist.
    """
    run = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Response run not found")

    if run.status in ("completed", "cancelled", "failed"):
        return CancelResponse(response_id=run.id, status=run.status)

    # Set cancellation signal in Redis for immediate worker break
    redis: Redis = request.app.state.redis
    try:
        await redis.set(f"{CANCEL_KEY_PREFIX}{response_id}", "1", ex=300)
    except Exception as exc:
        logger.warning("Failed to set Redis cancellation key: %s", exc)

    # Persist cancellation status and terminal stream event
    run.status = "cancelled"
    run.completed_at = func.now()  # type: ignore[assignment]

    last_seq = await session.scalar(
        select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
            StreamEvent.response_id == response_id
        )
    )
    new_seq = (last_seq or 0) + 1
    session.add(
        StreamEvent(
            response_id=response_id,
            seq=new_seq,
            event_type="status",
            event_id=make_event_id(response_id, new_seq),
            data={"status": "cancelled"},
        )
    )
    await session.commit()

    return CancelResponse(response_id=run.id, status="cancelled")
