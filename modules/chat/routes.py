"""Owner-authenticated HTTP routes for chat conversations, messages, SSE streaming, and run cancellation."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.dependencies import require_account, require_account_write
from core.auth.models import AuthSession
from core.database import get_session
from core.rate_limit import require_budget
from core.realtime.routes import _PermitResponse
from core.workspaces.dependencies import (
    require_default_workspace_read,
    require_default_workspace_write,
)
from core.workspaces.schemas import WorkspaceContext
from modules.chat import conversation_service, signals
from modules.chat import public as chat_public
from modules.chat._routes.guards import (
    _TOKEN_RE as _TOKEN_RE,
)
from modules.chat._routes.guards import (
    ACTIVE_RUN_STATUSES as ACTIVE_RUN_STATUSES,
)
from modules.chat._routes.guards import (
    DB_READ_TIMEOUT as DB_READ_TIMEOUT,
)
from modules.chat._routes.guards import (
    _auth_row_current as _auth_row_current,
)
from modules.chat._routes.guards import (
    _dispatch_response_run as _dispatch_response_run,
)
from modules.chat._routes.guards import (
    _filter_citation_lists as _filter_citation_lists,
)
from modules.chat._routes.guards import (
    _lock_conversation as _lock_conversation,
)
from modules.chat._routes.guards import (
    _message_mutation_digest as _message_mutation_digest,
)
from modules.chat._routes.guards import (
    _message_reasoning as _message_reasoning,
)
from modules.chat._routes.guards import (
    _mutation_model_override as _mutation_model_override,
)
from modules.chat._routes.guards import (
    _poll_needs_lock as _poll_needs_lock,
)
from modules.chat._routes.guards import (
    _privacy_fence as _privacy_fence,
)
from modules.chat._routes.guards import (
    _reject_active_response as _reject_active_response,
)
from modules.chat._routes.guards import (
    _reject_expired_conversation as _reject_expired_conversation,
)
from modules.chat._routes.guards import (
    _run_context as _run_context,
)
from modules.chat._routes.guards import (
    _run_scope as _run_scope,
)
from modules.chat._routes.guards import (
    _session_is_current as _session_is_current,
)
from modules.chat._routes.guards import (
    _token_hash as _token_hash,
)
from modules.chat._routes.guards import (
    _validate_model_pair as _validate_model_pair,
)
from modules.chat._routes.guards import (
    _validated_model_override as _validated_model_override,
)
from modules.chat._routes.mutation import (
    _load_mutation_originals as _load_mutation_originals,
)
from modules.chat._routes.mutation import (
    _message_reads as _message_reads,
)
from modules.chat._routes.mutation import (
    _mutation_digest_for as _mutation_digest_for,
)
from modules.chat._routes.mutation import (
    _replay_mutation_or_reject_collision as _replay_mutation_or_reject_collision,
)
from modules.chat._routes.mutation import (
    _require_history_storage as _require_history_storage,
)
from modules.chat._routes.mutation import (
    _stage_mutation_branch as _stage_mutation_branch,
)
from modules.chat._routes.stream import (
    _TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES,
)
from modules.chat._routes.stream import (
    HEARTBEAT_INTERVAL as HEARTBEAT_INTERVAL,
)
from modules.chat._routes.stream import (
    STREAM_BATCH_MAX_BYTES as STREAM_BATCH_MAX_BYTES,
)
from modules.chat._routes.stream import (
    STREAM_BATCH_SIZE as STREAM_BATCH_SIZE,
)
from modules.chat._routes.stream import (
    STREAM_POLL_INTERVAL as STREAM_POLL_INTERVAL,
)
from modules.chat._routes.stream import (
    STREAM_POLL_MAX_INTERVAL as STREAM_POLL_MAX_INTERVAL,
)
from modules.chat._routes.stream import (
    _authorize_stream_run as _authorize_stream_run,
)
from modules.chat._routes.stream import (
    _frames_for_batch as _frames_for_batch,
)
from modules.chat._routes.stream import (
    _idle_wait as _idle_wait,
)
from modules.chat._routes.stream import (
    _locked_poll as _locked_poll,
)
from modules.chat._routes.stream import (
    _poll_loop as _poll_loop,
)
from modules.chat._routes.stream import (
    _PollState as _PollState,
)
from modules.chat._routes.stream import (
    _publish_locked_events as _publish_locked_events,
)
from modules.chat._routes.stream import (
    _sse_event_stream as _sse_event_stream,
)
from modules.chat._routes.stream import (
    _stream_start_seq as _stream_start_seq,
)
from modules.chat._routes.support import (
    _redact_cancelled_run_in_fresh_session,
    _replayed_send_response,
)
from modules.chat.models import (
    Conversation,
    Message,
    ResponseRun,
)
from modules.chat.schemas import (
    CancelResponse,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    MessageMutationRequest,
    SendMessageRequest,
    SendMessageResponse,
    SubmitInputRequest,
    SubmitInputResponse,
)
from modules.chat.scope import multi_workspace_enabled, owner_default_scope
from modules.chat.stream import (
    format_sse_event as format_sse_event,  # re-exported: tests read routes.format_sse_event
)
from modules.chat.user_input import InputRequestError, submit_input
from modules.chat.worker import (
    CANCEL_KEY_PREFIX,
    _cancel_response_locked,
    _privacy_cancel_locked,
    _require_privacy_fence,
)
from modules.memory.public import lock_export_privacy, read_export_privacy
from modules.settings.public import module_dependency

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(module_dependency("chat"))])

Session = Annotated[AsyncSession, Depends(get_session)]


async def _account_read(
    auth: Annotated[AuthSession, Depends(require_account)],
    _default: Annotated[WorkspaceContext, Depends(require_default_workspace_read)],
) -> AuthSession:
    """Any active account, bound to its own default workspace (a foreign X-Workspace-ID returns 409)."""
    return auth


async def _account_write(
    auth: Annotated[AuthSession, Depends(require_account_write)],
    _default: Annotated[WorkspaceContext, Depends(require_default_workspace_write)],
) -> AuthSession:
    """CSRF-checked write by any active account under its own default workspace."""
    return auth


OwnerRead = Annotated[AuthSession, Depends(_account_read)]
OwnerWrite = Annotated[AuthSession, Depends(_account_write)]
MAX_CHAT_STREAMS_PER_API_PROCESS = 64
EPHEMERAL_TTL = timedelta(hours=24)


@router.get("/api/v1/conversations", response_model=list[ConversationRead])
async def list_conversations(
    session: Session,
    _owner: OwnerRead,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    archived: bool = False,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> list[ConversationRead]:
    """List persistent owner conversations ordered by most recent update.

    Args:
        session: Active asynchronous database session.
        _owner: Authenticated owner session dependency.
        response: HTTP response object to attach cache prevention headers.
        limit: Maximum number of conversations to return (1-100).
        offset: Offset for pagination.
        archived: Filter to archived or active conversations.
        q: Optional literal title substring; uses the retained-history search path.

    Returns:
        List of ConversationRead schemas.
    """
    response.headers["Cache-Control"] = "private, no-store"
    now = datetime.now(UTC)
    scope = await owner_default_scope(session, _owner.owner_id)
    if q is not None and not q.strip():
        raise HTTPException(status_code=422, detail="q must not be blank")
    rows = (
        await chat_public.search_conversations(
            session,
            q,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled(),
            limit=limit,
            offset=offset,
            archived=archived,
        )
        if q is not None
        else (
            await session.scalars(
                select(Conversation)
                .where(
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.archived == archived,
                )
                # Per-rule automation threads stay reachable from automation run detail, not the Chat list.
                .where(
                    or_(
                        Conversation.context_kind.is_(None),
                        Conversation.context_kind != "automation",
                    )
                )
                .where(or_(Conversation.ephemeral.is_(False), Conversation.expires_at > now))
                .order_by(
                    desc(Conversation.pinned), desc(Conversation.updated_at), desc(Conversation.id)
                )  # id breaks timestamp ties so OFFSET pages are stable
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )

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


@router.get(
    "/api/v1/conversations/{conversation_id}/agent-runs/{run_id}/activity",
    response_model=chat_public.AgentActivityRead,
)
async def read_agent_activity(
    conversation_id: UUID,
    run_id: UUID,
    session: Session,
    owner: OwnerRead,
) -> chat_public.AgentActivityRead:
    """Read bounded agent activity through its owner-checked chat conversation link."""
    scope = await owner_default_scope(session, owner.owner_id)
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == scope.workspace_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)
    return await chat_public.get_agent_activity(
        session,
        conversation_id,
        run_id,
        owner.owner_id,
        owner.token_hash,
        scope=scope,
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
    scope = await owner_default_scope(session, _owner.owner_id)
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    privacy = await read_export_privacy(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled()
    )
    is_ephemeral = not privacy.store_conversation_history
    conv = Conversation(
        workspace_id=scope.workspace_id,
        actor_user_id=scope.user_id,
        title=title,
        context_kind=payload.context_kind,
        context_resource_id=payload.context_resource_id,
        metadata_json=payload.metadata,
        ephemeral=is_ephemeral,
        expires_at=datetime.now(UTC) + EPHEMERAL_TTL if is_ephemeral else None,
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
    """Retrieve ordered messages with current citations and the durable response handle.

    An active response ID lets any owner-authorized Chat surface reattach to its replayable SSE
    stream after navigation or remount, without creating another message or generation run.

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
    scope = await owner_default_scope(session, _owner.owner_id)
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == scope.workspace_id,
        )
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conv)

    messages_rows, active_response_id = await conversation_service.load_transcript(
        session, conversation_id
    )
    (
        response_ids_by_user_message,
        web_search_by_assistant,
    ) = await conversation_service.load_run_links(
        session,
        conversation_id,
        messages_rows,
    )

    # One batched evidence lookup for the whole transcript instead of one per message (P2-6).
    current_citations = await _filter_citation_lists(
        session, [m.citations or [] for m in messages_rows], scope
    )
    messages_list = _message_reads(
        messages_rows,
        current_citations,
        response_ids_by_user_message,
        web_search_by_assistant,
    )
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
        active_response_id=active_response_id,
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
    scope = await owner_default_scope(session, _owner.owner_id)
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == scope.workspace_id,
        )
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conv)

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

    conv.updated_at = func.now()
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
    from modules.chat.public import delete_conversation as delete_chat_conversation

    deleted = await delete_chat_conversation(session, conversation_id, _owner.owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.commit()


@router.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=202,
    dependencies=[Depends(require_budget("chat_send", 30, 60))],
)
async def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    session: Session,
    _owner: OwnerWrite,
) -> SendMessageResponse:
    """Accept a user message, enforce client_request_id idempotency, and dispatch response generation.

    Locks the conversation while checking for a pending/streaming run and persisting the user
    message and ResponseRun. Repeated submissions with the same client_request_id return the
    existing response without duplicated generation.

    Args:
        conversation_id: UUID of the parent conversation.
        payload: SendMessageRequest containing message content, optional client request ID and context.
        request: FastAPI request providing access to background task scheduling and Redis.
        session: Active asynchronous database session.
        _owner: Authenticated owner write session dependency.

    Returns:
        SendMessageResponse with message_id, response_id, and pending/active status.

    Raises:
        HTTPException: 404 if the conversation is missing; 409 if another response is active.
    """
    scope = await owner_default_scope(session, _owner.owner_id)
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    conv = await _lock_conversation(session, conversation_id, scope)
    _reject_expired_conversation(conv)

    # Idempotency check with client_request_id
    replay = await _replayed_send_response(session, conversation_id, payload)
    if replay is not None:
        return replay

    privacy = await read_export_privacy(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled()
    )
    _require_history_storage(privacy, conv)

    await _reject_active_response(session, conversation_id)
    # Validated after the idempotent replay return so a replay never depends on current settings.
    model_override = await _validated_model_override(request, session, scope, payload)

    # Persist user message and pending response run before dispatch
    user_msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=payload.content.strip(),
        client_request_id=payload.client_request_id,
    )
    session.add(user_msg)
    await session.flush()

    resolved_context = await chat_public.resolve_gadget_context(
        session,
        payload.context,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled(),
    )
    await chat_public.reject_unsendable_selection(
        session,
        resolved_context,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled(),
    )
    response_run = ResponseRun(
        workspace_id=conv.workspace_id,
        actor_user_id=conv.actor_user_id,
        conversation_id=conversation_id,
        user_message_id=user_msg.id,
        client_request_id=payload.client_request_id,
        status="pending",
        retrieval_context=_run_context(
            resolved_context,
            privacy,
            payload.web_search,
            model_override,
            db_query=payload.db_query,
        ),
        ephemeral=conv.ephemeral,
        expires_at=conv.expires_at,
    )
    session.add(response_run)

    conv.updated_at = func.now()
    await session.commit()
    await session.refresh(response_run)

    await _dispatch_response_run(request, response_run.id)

    return SendMessageResponse(
        message_id=user_msg.id,
        response_id=response_run.id,
        status="pending",
    )


@router.post(
    "/api/v1/conversations/{conversation_id}/messages/{message_id}/mutations",
    response_model=SendMessageResponse,
    status_code=202,
    dependencies=[Depends(require_budget("chat_send", 30, 60))],
)
async def mutate_message(
    conversation_id: UUID,
    message_id: UUID,
    payload: MessageMutationRequest,
    request: Request,
    session: Session,
    _owner: OwnerWrite,
) -> SendMessageResponse:
    """Append an edited prompt or regenerated answer while retaining the original transcript.

    The conversation row serializes this operation with normal sends. A receipt and new
    ResponseRun commit atomically; identical retries return that run, while reusing the same
    request ID for different content is rejected. Retrieval context comes only from the
    original run and is revalidated by the worker before any model egress.

    Args:
        conversation_id: Parent conversation receiving the new branch.
        message_id: Existing user prompt to edit or assistant answer to regenerate.
        payload: Action, base-content digest, request identity, and replacement prompt if editing.
        request: App state providing ARQ, worker, and model configuration handles.
        session: Async transaction for parent lock, receipt lookup, and atomic inserts.
        _owner: Authenticated owner session with write/CSRF validation.

    Returns:
        Acknowledgment containing the appended prompt ID and durable response-run ID.

    Raises:
        HTTPException: 404 for unavailable messages, 409 for stale/idempotency/active-run
            conflicts, or 422 for action-role/content mismatches.
    """
    scope = await owner_default_scope(session, _owner.owner_id)
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    conversation = await _lock_conversation(session, conversation_id, scope)
    _reject_expired_conversation(conversation)
    normalized_content = payload.content.strip() if payload.content is not None else None
    if payload.action == "edit" and not normalized_content:
        raise HTTPException(status_code=422, detail="Editing requires non-empty prompt content")
    if payload.action == "regenerate" and payload.content is not None:
        raise HTTPException(status_code=422, detail="Regeneration reuses the original prompt")
    digest = _mutation_digest_for(payload, message_id, normalized_content)

    replay = await _replay_mutation_or_reject_collision(session, conversation_id, payload, digest)
    if replay is not None:
        return replay
    privacy = await read_export_privacy(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled()
    )
    _require_history_storage(privacy, conversation)
    await _reject_active_response(session, conversation_id)

    target, original_run, original_prompt = await _load_mutation_originals(
        session,
        conversation_id,
        message_id,
        payload,
    )
    # The stored fence is a snapshot: re-check its sources' current local_only before a new run exists.
    await chat_public.reject_unsendable_selection(
        session,
        original_run.retrieval_context,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled(),
    )

    # Explicit client choice wins; otherwise the original run's model is carried (revalidated, 422 if gone).
    model_override = await _mutation_model_override(
        request,
        session,
        scope,
        payload,
        original_run.retrieval_context,
    )

    user_message, response_run = await _stage_mutation_branch(
        session,
        conversation,
        payload,
        target=target,
        original_run=original_run,
        original_prompt=original_prompt,
        normalized_content=normalized_content,
        privacy=privacy,
        model_override=model_override,
        digest=digest,
    )
    await session.commit()

    await _dispatch_response_run(request, response_run.id)
    return SendMessageResponse(
        message_id=user_message.id,
        response_id=response_run.id,
        status="pending",
    )


@router.get(
    "/api/v1/responses/{response_id}/events", dependencies=[Depends(require_default_workspace_read)]
)
async def get_response_events(
    response_id: UUID,
    request: Request,
    request_session: Session,
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
        HTTPException: 401 if unauthenticated, 404 if run not found, 503 at the stream cap.
    """
    # The router's module/owner dependency read through this request-scoped session, whose yield
    # dependency only exits after the response ends: without this, every open stream would pin one
    # pooled connection idle in transaction (pool 10+10 < the 64-stream cap). The stream uses its own
    # short sessions from here on.
    await request_session.close()
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    await _authorize_stream_run(factory, request, response_id)

    start_seq = _stream_start_seq(last_event_id, cursor)
    token_hash = _token_hash(request)
    semaphore: asyncio.Semaphore = request.app.state.chat_streams
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.01)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Chat stream limit reached") from exc
    started = asyncio.Event()

    return _PermitResponse(
        _sse_event_stream(
            request,
            factory,
            response_id,
            token_hash,
            start_seq,
            started,
            semaphore,
            getattr(request.app.state, "redis", None),
        ),
        started,
        semaphore,
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
    run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
    if run_hint is None:
        raise HTTPException(status_code=404, detail="Response run not found")
    scope = await owner_default_scope(session, _owner.owner_id)
    if run_hint.workspace_id != scope.workspace_id or run_hint.actor_user_id != _owner.owner_id:
        raise HTTPException(status_code=404, detail="Response run not found")
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == run_hint.conversation_id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)

    # Set cancellation signal in Redis for immediate worker break
    redis: Redis = request.app.state.redis
    try:
        await redis.set(f"{CANCEL_KEY_PREFIX}{response_id}", "1", ex=300)
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Failed to set Redis cancellation key: %s", exc)

    # Memory -> parent -> run is the same lock order as worker publications. The response row
    # serializes status and terminal sequence allocation against a concurrent delta/redaction.
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == run_hint.conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)
    run = await session.scalar(
        select(ResponseRun)
        .where(
            ResponseRun.id == response_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Response run not found")
    if run.status in ("completed", "cancelled", "failed"):
        return CancelResponse(response_id=run.id, status=run.status)

    fence = (run.retrieval_context or {}).get("_chat_privacy_fence")
    try:
        await _require_privacy_fence(session, fence, scope=scope)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        try:
            await _privacy_cancel_locked(session, run)
            await session.commit()
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # A failed Memory read can leave this transaction aborted; retry durable redaction
            # under the same public Memory lock in a fresh Chat transaction before returning.
            await session.rollback()
            await _redact_cancelled_run_in_fresh_session(
                request.app.state.session_factory, scope.workspace_id, response_id
            )
    else:
        await _cancel_response_locked(session, run)
        await session.commit()
    # Wake streams after the terminal event is committed (a spurious wake-up is harmless).
    await signals.publish_response_signal(redis, response_id)
    return CancelResponse(response_id=run.id, status="cancelled")


@router.post("/api/v1/responses/{response_id}/input", response_model=SubmitInputResponse)
async def submit_response_input(
    response_id: UUID,
    body: SubmitInputRequest,
    session: Session,
    _owner: OwnerWrite,
) -> SubmitInputResponse:
    """Answer the pending `request_user_input` of a parked run and resume that same run.

    Owner-only and CSRF-checked (OwnerWrite). A foreign run or a request id that is not the open one is a
    404 (no existence oracle); a second answer or an answer after cancel is 409; an invalid slot/option/text
    is 422. An expired request cancels the run (committed) and returns 409 `expired`.

    Args:
        response_id: Parked ResponseRun.
        body: Request id and the answer string (slot id, option id, `__decline__` or text).
        session: Database session.
        _owner: Authenticated write session.

    Returns:
        The run id and request id; the waiting worker picks the answer up from the database.
    """
    scope = await owner_default_scope(session, _owner.owner_id)
    try:
        await submit_input(
            session,
            response_id=response_id,
            request_id=body.request_id,
            answer=body.answer,
            workspace_id=scope.workspace_id,
            actor_user_id=_owner.owner_id,
        )
    except InputRequestError as exc:
        if exc.code == "expired":
            await session.commit()  # persist the expiry/cancel the check performed
        else:
            await session.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await session.commit()
    return SubmitInputResponse(response_id=response_id, request_id=body.request_id)
