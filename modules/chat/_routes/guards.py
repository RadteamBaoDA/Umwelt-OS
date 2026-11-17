"""Chat route guards, run-context and session/auth currency helpers behind ``modules.chat.routes``."""

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.dependencies import SESSION_COOKIE
from core.auth.models import AuthSession
from core.errors import DomainError
from core.workspaces.schemas import WorkspaceContext
from modules.chat import conversation_service
from modules.chat import public as chat_public
from modules.chat.models import (
    Conversation,
    ResponseRun,
    StreamEvent,
)
from modules.chat.schemas import (
    DB_QUERY_KEY,
    GADGET_SCOPE_KEY,
    MODEL_OVERRIDE_KEY,
    SUGGESTIONS_KEY,
    WEB_SEARCH_KEY,
    MessageMutationRequest,
    SendMessageRequest,
)
from modules.chat.scope import multi_workspace_enabled, owner_default_scope
from modules.chat.worker import (
    CHAT_QUEUE,
)
from modules.memory.public import read_export_privacy

# Runs that count as active (defined with the queries that use it); re-exported for tests and SSE.
# Keep the original logger name so log routing and caplog filters stay unchanged after the split.
logger = logging.getLogger("modules.chat.routes")

ACTIVE_RUN_STATUSES = conversation_service.ACTIVE_RUN_STATUSES

_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
DB_READ_TIMEOUT = 3.0


def _privacy_fence(privacy: Any) -> dict[str, object]:
    """Serialize the public Memory consent snapshot onto a newly admitted response."""
    return {
        "store_conversation_history": privacy.store_conversation_history,
        "persisted": privacy.persisted,
        "updated_at": privacy.updated_at.isoformat() if privacy.updated_at is not None else None,
    }


def _reject_expired_conversation(conversation: Conversation) -> None:
    """Prevent an expired ephemeral conversation from being read or extended."""
    if conversation.ephemeral and (
        conversation.expires_at is None or conversation.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=410, detail="This temporary conversation has expired")


async def _lock_conversation(
    session: AsyncSession, conversation_id: UUID, scope: WorkspaceContext
) -> Conversation:
    """Take the Chat parent lock used to serialize message creation and mutation.

    The same parent-before-child order is used by conversation deletion. Every send or
    mutation must hold this row through its active-run check and durable insert so concurrent
    requests cannot both observe an idle conversation.
    """
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.workspace_id == scope.workspace_id)
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
            ResponseRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    )
    if active_id is not None:
        raise HTTPException(
            status_code=409, detail="A response is already active for this conversation"
        )


async def _dispatch_response_run(request: Request, response_id: UUID) -> None:
    """Enqueue an already committed response run on the dedicated chat worker queue.

    PostgreSQL holds the durable run row; a lost push is replayed by `recover_chat_runs`.
    """
    try:
        await request.app.state.redis.enqueue_job(
            "process_chat_response",
            str(response_id),
            _job_id=f"chat-response:{response_id}",
            _queue_name=CHAT_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Failed to enqueue chat response %s (%s)", response_id, type(exc).__name__)


def _run_context(
    inherited: dict[str, Any] | None,
    privacy: Any,
    web_search: bool,
    model_override: dict[str, str] | None = None,
    *,
    db_query: bool = False,
) -> dict[str, Any]:
    """Build a run's retrieval context: client-visible keys kept, every server-private "_" key rewritten.

    Stale "_" keys (old privacy fence, web opt-in, web outcome) are never inherited, so each send,
    regenerate and edit carries only its own fence and its own explicit web-search choice.
    """
    kept = {k: v for k, v in (inherited or {}).items() if not k.startswith("_")}
    context: dict[str, Any] = {
        **kept,
        "_chat_privacy_fence": _privacy_fence(privacy),
        WEB_SEARCH_KEY: {"requested": web_search},
    }
    is_gadget = kept.get("kind") == "gadget"
    if db_query or is_gadget:
        context[DB_QUERY_KEY] = {"requested": True, "auto_gadget": is_gadget}
    if is_gadget:
        context[GADGET_SCOPE_KEY] = {
            "gadget_id": kept.get("gadget_id") or "",
            "title": kept.get("title") or "",
            "renderer": kept.get("renderer") or "",
            "source_scope": kept.get("source_scope") or [],
            "scope": kept.get("gadget_scope") or {},
        }
    if model_override:
        context[MODEL_OVERRIDE_KEY] = model_override
    return context


async def _validate_model_pair(
    request: Request,
    session: AsyncSession,
    scope: Any,
    provider_id: str,
    model: str,
) -> dict[str, str]:
    """Validate one provider/model pair against the owner's current execution config.

    The provider must exist and not be policy-denied, and the model must be one of its configured models
    that the `chat` role accepts (type `llm`). Anything else is a 422 so a stale picker never silently
    falls back to a different model.

    Raises:
        HTTPException: 422 when the provider or model is unknown, denied, or not a chat-capable model.
    """
    from modules.settings import public as settings_public

    config = await settings_public.get_ai_execution_config(
        session,
        request.app.state.settings,
        request.app.state.redis,
        scope=scope,
    )
    provider = config.providers.get(provider_id)
    chosen = next((m for m in provider.models if m.id == model), None) if provider else None
    if (
        provider is None
        or provider.policy_denied
        or chosen is None
        or not settings_public.role_accepts("chat", chosen)
    ):
        raise HTTPException(status_code=422, detail="Selected chat model is not available")
    return {"provider_id": provider_id, "model": model}


async def _validated_model_override(
    request: Request,
    session: AsyncSession,
    scope: Any,
    payload: SendMessageRequest | MessageMutationRequest,
) -> dict[str, str] | None:
    """Return the validated per-request chat model override, or None when the owner's default applies.

    Raises:
        HTTPException: 422 when the provider or model is unknown, denied, or not a chat-capable model.
    """
    if payload.model is None or payload.provider_id is None:
        return None
    return await _validate_model_pair(request, session, scope, payload.provider_id, payload.model)


async def _mutation_model_override(
    request: Request,
    session: AsyncSession,
    scope: Any,
    payload: MessageMutationRequest,
    original_context: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Pick the model for a regenerate/edit run: an explicit payload choice wins, else the original run's.

    `_run_context` strips server-private "_" keys, so the original run's override is carried forward
    here and revalidated exactly like a fresh send; a model that is no longer valid is a 422 rather
    than a silent switch to a different model.

    Raises:
        HTTPException: 422 when the chosen or carried model is no longer available.
    """
    explicit = await _validated_model_override(request, session, scope, payload)
    if explicit is not None:
        return explicit
    carried = (original_context or {}).get(MODEL_OVERRIDE_KEY)
    if not isinstance(carried, dict):
        return None
    provider_id, model = carried.get("provider_id"), carried.get("model")
    if not isinstance(provider_id, str) or not isinstance(model, str):
        return None
    return await _validate_model_pair(request, session, scope, provider_id, model)


def _message_reasoning(metadata: Any) -> str | None:
    """Return the stored model reasoning text from a message's metadata, or None when absent or malformed."""
    value = metadata.get("reasoning") if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value else None


def _message_uncited(metadata: Any) -> bool:
    """True when the assistant answer was stored without any verified citation (``metadata.uncited``)."""
    return isinstance(metadata, dict) and metadata.get("uncited") is True


def _message_suggestions(metadata: Any) -> list[str]:
    """Return the stored follow-up suggestions from a message's metadata; malformed values yield ``[]``."""
    value = metadata.get(SUGGESTIONS_KEY) if isinstance(metadata, dict) else None
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, str) and s][:3]


def _message_mutation_digest(
    *,
    action: str,
    target_message_id: UUID,
    base_content_hash: str,
    content: str | None,
    web_search: bool = False,
    model_override: dict[str, str] | None = None,
) -> str:
    """Hash canonical mutation fields so a request key cannot be replayed with new data.

    Args:
        action: Edit or regenerate operation selected by the user.
        target_message_id: Immutable transcript entry acted upon.
        base_content_hash: SHA-256 of the displayed message used for the concurrency fence.
        content: Trimmed replacement text for edits, or None for regeneration.
        web_search: Per-run web search opt-in; hashed only when true so pre-deploy receipts stay valid.
        model_override: Explicit client model choice; hashed only when present for the same reason.

    Returns:
        Lowercase SHA-256 digest of the canonical JSON payload.
    """
    fields: dict[str, object] = {
        "action": action,
        "target_message_id": str(target_message_id),
        "base_content_hash": base_content_hash,
        "content": content,
    }
    if web_search:
        fields["web_search"] = True
    if model_override:
        fields["model_override"] = model_override
    canonical = json.dumps(
        fields,
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


async def _session_is_current(request: Request) -> int | None:
    """Admit only a live session whose account still has its active default workspace.

    Returns:
        The requester's account id; None when missing, expired, disabled or without a default workspace.

    Raises:
        HTTPException: 503 when session verification times out.
    """
    th = _token_hash(request)
    if th is None or not _TOKEN_RE.fullmatch(th):
        return None
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        async with asyncio.timeout(DB_READ_TIMEOUT):
            async with factory() as session:
                return await _auth_row_current(session, th)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Session verification temporarily unavailable"
        ) from exc


async def _auth_row_current(session: AsyncSession, token_hash: str | None) -> int | None:
    """Account + default-workspace admission of the hashed session token; the requester's account id or None."""
    from core.auth.public import revalidate_account_session

    if token_hash is None or not _TOKEN_RE.fullmatch(token_hash):
        return None
    account_id = await session.scalar(
        select(AuthSession.owner_id)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.expires_at > datetime.now(UTC),
        )
        .limit(1)
    )
    if account_id is None or not await revalidate_account_session(
        session,
        token_hash,
        account_id,
        multi_workspace_enabled=multi_workspace_enabled(),
    ):
        return None
    try:
        await owner_default_scope(session, account_id)
    except (HTTPException, DomainError):
        return None
    return int(account_id)


async def _run_scope(session: AsyncSession, run: ResponseRun) -> WorkspaceContext:
    """The run actor's default-workspace scope; 404 unless it is the workspace the run was stamped with."""
    scope = await owner_default_scope(session, run.actor_user_id)
    if scope.workspace_id != run.workspace_id:
        raise HTTPException(status_code=404, detail="Response run not found")
    return scope


async def _poll_needs_lock(
    session: AsyncSession,
    response_id: UUID,
    current_seq: int,
    token_hash: str | None,
) -> bool:
    """Lock-free pre-check for one SSE poll: is there an event to publish or a reason to close the stream?

    Idle polls of active runs otherwise each took the GLOBAL Memory privacy key plus Conversation/Run
    FOR UPDATE just to find nothing; 64 idle streams serialized on that key and exhausted the pool
    (P14-T4 integration). This reads committed state only and publishes nothing, so I1-I5 are unchanged:
    every write, and every terminal decision (missing/expired run or parent, fence change, revoked
    session), still happens in the locked transaction, which re-checks all of it. A change committed
    after this probe is seen by the next poll, as before.
    """
    run = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
    if run is None or run.status not in ACTIVE_RUN_STATUSES:
        return True
    newer = await session.scalar(
        select(StreamEvent.seq)
        .where(
            StreamEvent.response_id == response_id,
            StreamEvent.seq > current_seq,
        )
        .limit(1)
    )
    if newer is not None:
        return True
    parent = await session.scalar(
        select(Conversation).where(Conversation.id == run.conversation_id)
    )
    if parent is None or (
        parent.ephemeral and (parent.expires_at is None or parent.expires_at <= datetime.now(UTC))
    ):
        return True
    try:
        current = _privacy_fence(
            await read_export_privacy(
                session,
                scope=await _run_scope(session, run),
                multi_workspace_enabled=multi_workspace_enabled(),
            )
        )
    except Exception:  # noqa: BLE001  # deliberate boundary: the locked path re-reads and cancels/reports
        return True
    stamp = (run.retrieval_context or {}).get("_chat_privacy_fence")
    if stamp != current:
        return True
    account_id = await _auth_row_current(session, token_hash)
    return account_id is None or run.actor_user_id != account_id


async def _filter_citation_lists(
    session: AsyncSession,
    lists: list[object],
    scope: WorkspaceContext,
) -> list[list[dict[str, object]]]:
    """Filter many citation lists with ONE `filter_current_citations` call (one evidence lock per 100 refs).

    `filter_current_citations` returns the kept raw dicts themselves and keeps or drops each citation
    independently of the others, so mapping kept objects back by identity reproduces exactly the
    per-list output, in each list's original order. Non-list inputs yield [] as before.
    """
    flat = [citation for raw in lists if isinstance(raw, list) for citation in raw]
    kept = {
        id(citation)
        for citation in await chat_public.filter_current_citations(
            session,
            flat,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled(),
        )
    }
    return [[c for c in raw if id(c) in kept] if isinstance(raw, list) else [] for raw in lists]
