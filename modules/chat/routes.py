"""Owner-authenticated HTTP routes for chat conversations, messages, SSE streaming, and run cancellation."""

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import logging
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.dependencies import SESSION_COOKIE, require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.chat.models import Conversation, Message, MessageMutationReceipt, ResponseRun, StreamEvent
from modules.chat import public as chat_public
from modules.settings.public import module_dependency
from modules.memory.public import lock_export_privacy, read_export_privacy
from modules.chat.schemas import (
    CancelResponse,
    ConversationCreate,
    ConversationDetailRead,
    ConversationPatch,
    ConversationRead,
    MessageMutationRequest,
    MessageRead,
    SendMessageRequest,
    SendMessageResponse,
)
from modules.chat.stream import format_sse_event, make_event_id, parse_event_id
from modules.chat.worker import (
    CANCEL_KEY_PREFIX,
    _cancel_response_locked,
    _mark_privacy_cancelled,
    _privacy_cancel_locked,
    _require_privacy_fence,
    run_response_generation,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(module_dependency("chat"))])

Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]

_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
DB_READ_TIMEOUT = 3.0
STREAM_POLL_INTERVAL = 0.1
HEARTBEAT_INTERVAL = 15.0
EPHEMERAL_TTL = timedelta(hours=24)


def _privacy_fence(privacy: Any) -> dict[str, object]:
    """Serialize the public Memory consent snapshot onto a newly admitted response."""
    return {
        "store_conversation_history": privacy.store_conversation_history,
        "persisted": privacy.persisted,
        "updated_at": privacy.updated_at.isoformat() if privacy.updated_at is not None else None,
    }


def _reject_expired_conversation(conversation: Conversation) -> None:
    """Prevent an expired ephemeral conversation from being read or extended."""
    if (conversation.ephemeral and (conversation.expires_at is None
                                    or conversation.expires_at <= datetime.now(UTC))):
        raise HTTPException(status_code=410, detail="This temporary conversation has expired")


async def _lock_conversation(session: AsyncSession, conversation_id: UUID) -> Conversation:
    """Take the Chat parent lock used to serialize message creation and mutation.

    The same parent-before-child order is used by conversation deletion. Every send or
    mutation must hold this row through its active-run check and durable insert so concurrent
    requests cannot both observe an idle conversation.
    """
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


async def _reject_active_response(session: AsyncSession, conversation_id: UUID) -> None:
    """Reject a second turn while this conversation already owns an uncompleted response.

    Args:
        session: Transaction that already holds the conversation row lock.
        conversation_id: Conversation whose pending/streaming runs must be checked.
    """
    active_id = await session.scalar(
        select(ResponseRun.id)
        .where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.status.in_(("pending", "streaming")),
        )
        .limit(1)
    )
    if active_id is not None:
        raise HTTPException(status_code=409, detail="A response is already active for this conversation")


async def _dispatch_response_run(request: Request, response_id: UUID) -> None:
    """Enqueue and locally schedule an already committed response run.

    The durable run row is created by the caller before dispatch. Keeping both delivery
    mechanisms here preserves normal-send behavior while allowing append-only revisions to
    use the same worker and event stream.
    """
    redis: Redis = request.app.state.redis
    if hasattr(redis, "enqueue_job"):
        try:
            await redis.enqueue_job(
                "process_chat_response",
                str(response_id),
                _job_id=f"chat-response:{response_id}",
            )
        except Exception as exc:
            logger.warning("Failed to enqueue ARQ job: %s", exc)

    asyncio.create_task(
        run_response_generation(
            response_id=response_id,
            session_factory=request.app.state.session_factory,
            settings=request.app.state.settings,
            redis=redis,
        )
    )


def _message_mutation_digest(
    *, action: str, target_message_id: UUID, base_content_hash: str, content: str | None,
) -> str:
    """Hash canonical mutation fields so a request key cannot be replayed with new data.

    Args:
        action: Edit or regenerate operation selected by the user.
        target_message_id: Immutable transcript entry acted upon.
        base_content_hash: SHA-256 of the displayed message used for the concurrency fence.
        content: Trimmed replacement text for edits, or None for regeneration.

    Returns:
        Lowercase SHA-256 digest of the canonical JSON payload.
    """
    canonical = json.dumps(
        {
            "action": action,
            "target_message_id": str(target_message_id),
            "base_content_hash": base_content_hash,
            "content": content,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.archived == archived)
            # Per-rule automation threads stay reachable from automation run detail, not the Chat list.
            .where(or_(Conversation.context_kind.is_(None), Conversation.context_kind != "automation"))
            .where(or_(Conversation.ephemeral.is_(False), Conversation.expires_at > now))
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
    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)
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
    await lock_export_privacy(session)
    privacy = await read_export_privacy(session)
    is_ephemeral = not privacy.store_conversation_history
    conv = Conversation(
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
    """Retrieve ordered messages and the durable handle for a pending/streaming response.

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
    conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conv)

    messages_rows = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
    ).all()

    # Check for an active run after reading messages. If completion raced with that first read,
    # the active-run query sees no run and the terminal-run query below triggers a transcript
    # reload. If completion happens after the active-run query, returning its ID is still safe:
    # the replayed SSE delivers completion and invalidates this detail query.
    active_response_id = await session.scalar(
        select(ResponseRun.id)
        .where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.status.in_(("pending", "streaming")),
            ResponseRun.assistant_message_id.is_(None),
        )
        .order_by(desc(ResponseRun.created_at), desc(ResponseRun.id))
        .limit(1)
    )
    if active_response_id is None:
        latest_run = await session.scalar(
            select(ResponseRun)
            .where(ResponseRun.conversation_id == conversation_id)
            .order_by(desc(ResponseRun.created_at), desc(ResponseRun.id))
            .limit(1)
        )
        if (
            latest_run is not None
            and latest_run.status == "completed"
            and latest_run.assistant_message_id is not None
            and all(message.id != latest_run.assistant_message_id for message in messages_rows)
        ):
            messages_rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.asc())
                )
            ).all()

    response_ids_by_user_message: dict[UUID, UUID] = {}
    if messages_rows:
        response_rows = await session.execute(
            select(ResponseRun.user_message_id, ResponseRun.id).where(
                ResponseRun.user_message_id.in_([message.id for message in messages_rows])
            )
        )
        response_ids_by_user_message = {
            user_message_id: response_id
            for user_message_id, response_id in response_rows.all()
        }

    messages_list = [
        MessageRead(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            client_request_id=m.client_request_id,
            model_identity=m.model_identity,
            citations=m.citations or [],
            response_id=m.response_id or response_ids_by_user_message.get(m.id),
            revision_of_message_id=m.revision_of_message_id,
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
    conv = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
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
    await lock_export_privacy(session)
    conv = await _lock_conversation(session, conversation_id)
    _reject_expired_conversation(conv)

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

    privacy = await read_export_privacy(session)
    if not privacy.store_conversation_history and not conv.ephemeral:
        raise HTTPException(
            status_code=409,
            detail="History storage is disabled. Start a new temporary conversation to continue.",
        )

    await _reject_active_response(session, conversation_id)

    # Persist user message and pending response run before dispatch
    user_msg = Message(
        conversation_id=conversation_id,
        role="user",
        content=payload.content.strip(),
        client_request_id=payload.client_request_id,
    )
    session.add(user_msg)
    await session.flush()

    resolved_context = await chat_public.resolve_gadget_context(session, payload.context)
    response_run = ResponseRun(
        conversation_id=conversation_id,
        user_message_id=user_msg.id,
        client_request_id=payload.client_request_id,
        status="pending",
        retrieval_context={**resolved_context, "_chat_privacy_fence": _privacy_fence(privacy)},
        ephemeral=conv.ephemeral,
        expires_at=conv.expires_at,
    )
    session.add(response_run)

    conv.updated_at = func.now()  # type: ignore[assignment]
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
    await lock_export_privacy(session)
    conversation = await _lock_conversation(session, conversation_id)
    _reject_expired_conversation(conversation)
    normalized_content = payload.content.strip() if payload.content is not None else None
    if payload.action == "edit" and not normalized_content:
        raise HTTPException(status_code=422, detail="Editing requires non-empty prompt content")
    if payload.action == "regenerate" and payload.content is not None:
        raise HTTPException(status_code=422, detail="Regeneration reuses the original prompt")
    digest = _message_mutation_digest(
        action=payload.action,
        target_message_id=message_id,
        base_content_hash=payload.base_content_hash,
        content=normalized_content,
    )

    receipt = await session.scalar(
        select(MessageMutationReceipt).where(
            MessageMutationReceipt.conversation_id == conversation_id,
            MessageMutationReceipt.client_request_id == payload.client_request_id,
        )
    )
    if receipt is not None:
        if not hmac.compare_digest(receipt.request_digest, digest):
            raise HTTPException(status_code=409, detail="Mutation request ID was already used with different content")
        replayed_run = await session.scalar(select(ResponseRun).where(ResponseRun.id == receipt.response_id))
        if replayed_run is None:
            raise HTTPException(status_code=409, detail="Mutation receipt is no longer available")
        return SendMessageResponse(
            message_id=receipt.result_user_message_id,
            response_id=replayed_run.id,
            status=replayed_run.status,
        )

    # A request key belongs to one creation path; reject a collision with a normal send.
    collision = await session.scalar(
        select(ResponseRun.id).where(
            ResponseRun.conversation_id == conversation_id,
            ResponseRun.client_request_id == payload.client_request_id,
        ).limit(1)
    )
    if collision is not None:
        raise HTTPException(status_code=409, detail="Mutation request ID is already in use")
    privacy = await read_export_privacy(session)
    if not privacy.store_conversation_history and not conversation.ephemeral:
        raise HTTPException(
            status_code=409,
            detail="History storage is disabled. Start a new temporary conversation to continue.",
        )
    await _reject_active_response(session, conversation_id)

    target = await session.scalar(
        select(Message)
        .where(Message.id == message_id, Message.conversation_id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    expected_role = "user" if payload.action == "edit" else "assistant"
    if target.role != expected_role:
        raise HTTPException(status_code=422, detail="Mutation action does not match the message role")
    actual_hash = hashlib.sha256(target.content.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, payload.base_content_hash):
        raise HTTPException(status_code=409, detail="Message changed; reload the conversation before retrying")

    original_run_statement = select(ResponseRun).where(
        ResponseRun.conversation_id == conversation_id,
        (ResponseRun.user_message_id == target.id)
        if payload.action == "edit"
        else (ResponseRun.id == target.response_id),
    )
    if payload.action == "regenerate":
        original_run_statement = original_run_statement.where(ResponseRun.assistant_message_id == target.id)
    original_run = await session.scalar(original_run_statement.order_by(desc(ResponseRun.created_at)).limit(1))
    if original_run is None:
        raise HTTPException(status_code=409, detail="The original response context is unavailable")
    original_prompt = await session.scalar(
        select(Message).where(
            Message.id == original_run.user_message_id,
            Message.conversation_id == conversation_id,
            Message.role == "user",
        )
    )
    if original_prompt is None:
        raise HTTPException(status_code=409, detail="The original prompt is unavailable")

    user_message = Message(
        conversation_id=conversation_id,
        role="user",
        content=normalized_content if payload.action == "edit" else original_prompt.content,
        client_request_id=payload.client_request_id,
        revision_of_message_id=target.id,
    )
    session.add(user_message)
    await session.flush()
    response_run = ResponseRun(
        conversation_id=conversation_id,
        user_message_id=user_message.id,
        client_request_id=payload.client_request_id,
        status="pending",
        # Retain the original captured context byte-for-byte. The worker rechecks its
        # source/version fences before retrieval and before remote send.
        retrieval_context={
            **dict(original_run.retrieval_context or {}),
            "_chat_privacy_fence": _privacy_fence(privacy),
        },
        ephemeral=conversation.ephemeral,
        expires_at=conversation.expires_at,
    )
    session.add(response_run)
    await session.flush()
    session.add(MessageMutationReceipt(
        conversation_id=conversation_id,
        client_request_id=payload.client_request_id,
        request_digest=digest,
        action=payload.action,
        target_message_id=target.id,
        result_user_message_id=user_message.id,
        response_id=response_run.id,
    ))
    conversation.updated_at = func.now()  # type: ignore[assignment]
    await session.commit()

    await _dispatch_response_run(request, response_run.id)
    return SendMessageResponse(
        message_id=user_message.id,
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
        parent = await session.scalar(select(Conversation).where(Conversation.id == run.conversation_id))
        if parent is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _reject_expired_conversation(parent)

    selected_cursor = last_event_id or cursor
    start_seq = 1
    if selected_cursor:
        try:
            _, parsed_seq = parse_event_id(selected_cursor)
            start_seq = parsed_seq + 1
        except ValueError:
            start_seq = 1

    async def sse_event_stream() -> Any:
        """Yield one locked event at a time after auth, expiry, and active-run consent checks.

        The short per-event transaction stays open through the ASGI send of that single payload,
        serializing opt-out/redaction against publication without holding a lock over model streaming.
        Completed runs remain readable as previously retained transcript history.
        """
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

            event_sent = False
            terminal_without_event = False
            try:
                async with factory() as session:
                    run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
                    if run_hint is None:
                        return
                    # Terminal runs contain previously retained history; only active replay needs
                    # the owner privacy lock, and terminal state cannot transition back to active.
                    if run_hint.status in ("pending", "streaming"):
                        await lock_export_privacy(session)
                    parent = await session.scalar(select(Conversation).where(
                        Conversation.id == run_hint.conversation_id,
                    ).with_for_update().execution_options(populate_existing=True))
                    if parent is None:
                        return
                    if parent.ephemeral and (parent.expires_at is None
                                             or parent.expires_at <= datetime.now(UTC)):
                        return
                    current_run = await session.scalar(select(ResponseRun).where(
                        ResponseRun.id == response_id,
                    ).with_for_update().execution_options(populate_existing=True))
                    if current_run is None:
                        return
                    if current_run.status in ("pending", "streaming"):
                        stamp = (current_run.retrieval_context or {}).get("_chat_privacy_fence")
                        try:
                            await _require_privacy_fence(session, stamp)
                        except Exception:
                            # A failed read can poison the transaction; release locks and retry
                            # redaction in a fresh transaction before emitting only terminal status.
                            await session.rollback()
                            await _mark_privacy_cancelled(response_id, factory, current_seq)
                            yield format_sse_event("status", {"status": "cancelled"})
                            return

                    event = await session.scalar(
                        select(StreamEvent)
                        .where(
                            StreamEvent.response_id == response_id,
                            StreamEvent.seq > current_seq,
                        )
                        .order_by(StreamEvent.seq.asc())
                        .limit(1)
                    )
                    if event is not None:
                        # Do not materialize a batch: cancellation must never replay a stale object
                        # loaded before its payload was redacted by another transaction.
                        yield format_sse_event(
                            event=event.event_type,
                            data=event.data,
                            event_id=event.event_id,
                        )
                        current_seq = event.seq
                        await session.commit()
                        event_sent = True
                    else:
                        terminal_without_event = current_run.status in ("completed", "cancelled", "failed")
                        await session.commit()
            except Exception as exc:
                logger.warning("Error querying stream events: %s", type(exc).__name__)
                yield format_sse_event("status", {"status": "unavailable"})
                return

            if event_sent:
                last_heartbeat = asyncio.get_running_loop().time()
            elif terminal_without_event:
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
    run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
    if run_hint is None:
        raise HTTPException(status_code=404, detail="Response run not found")
    conversation = await session.scalar(select(Conversation).where(Conversation.id == run_hint.conversation_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)

    # Set cancellation signal in Redis for immediate worker break
    redis: Redis = request.app.state.redis
    try:
        await redis.set(f"{CANCEL_KEY_PREFIX}{response_id}", "1", ex=300)
    except Exception as exc:
        logger.warning("Failed to set Redis cancellation key: %s", exc)

    # Memory -> parent -> run is the same lock order as worker publications. The response row
    # serializes status and terminal sequence allocation against a concurrent delta/redaction.
    await lock_export_privacy(session)
    conversation = await session.scalar(select(Conversation).where(
        Conversation.id == run_hint.conversation_id,
    ).with_for_update().execution_options(populate_existing=True))
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _reject_expired_conversation(conversation)
    run = await session.scalar(select(ResponseRun).where(
        ResponseRun.id == response_id,
    ).with_for_update().execution_options(populate_existing=True))
    if run is None:
        raise HTTPException(status_code=404, detail="Response run not found")
    if run.status in ("completed", "cancelled", "failed"):
        return CancelResponse(response_id=run.id, status=run.status)

    fence = (run.retrieval_context or {}).get("_chat_privacy_fence")
    try:
        await _require_privacy_fence(session, fence)
    except Exception:
        try:
            await _privacy_cancel_locked(session, run)
            await session.commit()
        except Exception:
            # A failed Memory read can leave this transaction aborted; retry durable redaction
            # under the same public Memory lock in a fresh Chat transaction before returning.
            await session.rollback()
            async with request.app.state.session_factory() as redaction_session:
                await lock_export_privacy(redaction_session)
                current_hint = await redaction_session.scalar(select(ResponseRun).where(
                    ResponseRun.id == response_id,
                ))
                if current_hint is not None:
                    await redaction_session.scalar(select(Conversation).where(
                        Conversation.id == current_hint.conversation_id,
                    ).with_for_update().execution_options(populate_existing=True))
                    current_run = await redaction_session.scalar(select(ResponseRun).where(
                        ResponseRun.id == response_id,
                    ).with_for_update().execution_options(populate_existing=True))
                    if current_run is not None:
                        await _privacy_cancel_locked(redaction_session, current_run)
                await redaction_session.commit()
    else:
        await _cancel_response_locked(session, run)
        await session.commit()
    return CancelResponse(response_id=run.id, status="cancelled")
