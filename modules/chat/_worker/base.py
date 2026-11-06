"""Chat worker foundations: tuning constants, fence exceptions, job admission and the shared run-fence helpers."""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import DomainError
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.chat.models import (
    Conversation,
    ResponseRun,
    StreamEvent,
)
from modules.chat.scope import (
    multi_workspace_enabled,
    owner_default_scope,
)
from modules.memory.public import lock_export_privacy, read_export_privacy

logger = logging.getLogger("modules.chat.worker")


CANCEL_KEY_PREFIX = "chat:cancel:"


_CHAT_ROLE = "chat"  # model role of the answer stream


_CLAIM_SEQ = 1  # seq of the claim's first ``status`` event


# 0.4 s keeps SSE perceptibly live while cutting delta rows (each flush is one locked transaction) ~4x.
STREAM_FLUSH_SECONDS = 0.4


# Terminal runs keep their full text in message.done, so their deltas are dead weight after a day.
DELTA_RETENTION = timedelta(days=1)


# Model "thinking" kept on the assistant message; longer reasoning is cut and flagged `reasoning_truncated`.
REASONING_MAX_CHARS = 32_000


DELTA_PURGE_LIMIT = 5000


STREAM_FLUSH_CHARS = 256


CHAT_QUEUE = "arq:chat"


CHAT_JOB_TIMEOUT = 600  # arq job_timeout for ChatWorkerSettings (single source)


SHUTDOWN_TIMEOUT_MARGIN = 30.0


RECOVER_PENDING_AFTER = timedelta(seconds=15)


RECOVER_PENDING_MAX_AGE = timedelta(
    minutes=15
)  # (measured from updated_at) pending runs fail instead of re-enqueueing forever


CANCEL_CHECK_INTERVAL = 0.25  # per-line Redis cancel check throttle; flushes always check


RECOVER_STREAMING_AFTER = timedelta(seconds=660)  # arq job_timeout 600 s plus margin


EPHEMERAL_TTL = timedelta(hours=24)


SHUTDOWN_RELEASE_TIMEOUT = (
    5.0  # compose stop_grace_period is 30 s; arq awaits the job task before closing
)


WEB_SEARCH_STOP_SECONDS = (
    3.0  # search cleanup is a rollback; keeps stop + shutdown release (3 + 5) inside 10 s
)


# Web search egress fence: lock wait + DNS + connect + request body, while the privacy key is held (review P2-1).
WEB_SEARCH_FENCE_SECONDS = 4.0


WEB_SEARCH_NO_RESULTS_PROMPT = "Web search returned no results; do not cite the web."


WEB_SEARCH_UNAVAILABLE_PROMPT = "Web search was requested but is unavailable for this answer; do not claim to have searched the web."


_WEB_UNAVAILABLE_CODES = frozenset({"timeout", "provider_error", "network_denied"})


# read_skill is non-terminal: bound the extra model rounds and calls it may cost one turn.
MAX_SKILL_ROUNDS = 2


MAX_SKILL_CALLS_PER_ROUND = 2


MAX_TRIP_IMAGE_CALLS_PER_ROUND = 1  # get_trip_stop_images fans out to 8 lookups; one call per round


MAX_USER_INPUT_REQUESTS_PER_TURN = (
    2  # request_user_input parks the run; bound how often one turn may ask
)


# A sandbox widget's parameter keys; the first appearance of each is a streaming milestone (css done, html begun, ...).
_UI_MILESTONE_KEYS = ('"css"', '"html"', '"jsFunctions"', '"jsExpressions"')


class PrivacyFenceChanged(RuntimeError):
    """Response was admitted under a different or unavailable history-consent snapshot."""


class ResponseNoLongerActive(RuntimeError):
    """Another serialized action completed or cancelled this response before publication."""


async def _job_scope(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int
) -> InternalJobScope:
    """Durable-job subject for a run's stamped workspace; no admission lock is taken.

    Raises:
        PrivacyFenceChanged: The workspace owner is not the stamped actor or is no longer active.
    """
    try:
        owner = await workspaces.resolve_workspace_owner_context(
            session,
            workspace_id,
            multi_workspace_enabled=multi_workspace_enabled(),
        )
    except (HTTPException, DomainError) as exc:
        raise PrivacyFenceChanged("Chat owner is unavailable") from exc
    if owner is None or owner.user_id != actor_user_id:
        raise PrivacyFenceChanged("Chat owner is no longer the run actor")
    return InternalJobScope(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        membership_revision=owner.membership_revision,
    )


async def _admit_job(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    actor_user_id: int,
) -> tuple[InternalJobScope, AccessFence]:
    """Admit the run's actor in its stamped workspace and snapshot the access fence (D6).

    The fence is carried in worker memory and compared under lock at every send and publication.
    The transaction is rolled back before any model I/O.

    Raises:
        PrivacyFenceChanged: Admission was denied or the actor/workspace pair no longer matches.
    """
    async with session_factory() as session:
        scope = await _job_scope(session, workspace_id, actor_user_id)
        try:
            fence = await workspaces.authorize_internal_job(
                session,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled(),
            )
        except (HTTPException, DomainError) as exc:
            raise PrivacyFenceChanged("Chat job admission denied") from exc
        await session.rollback()
    return scope, fence


async def _scope_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    actor_user_id: int,
) -> InternalJobScope | None:
    """Best-effort job scope for terminal/recovery paths; None sends them to the redaction fallback."""
    try:
        async with session_factory() as session:
            return await _job_scope(session, workspace_id, actor_user_id)
    except Exception:  # noqa: BLE001  # boundary: no scope means terminal paths redact instead of read consent
        return None


def _day_scope(context: dict[str, Any], metadata: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return a validated (ISO date, IANA timezone) day scope, or None when absent or malformed."""
    # The conversation's stored day wins; the client value applies only when none is stored.
    for source in (metadata or {}, context if context.get("kind") == "day" else {}):
        try:
            day, zone = str(source["date"]), str(source["timezone"])
            date.fromisoformat(day)
            ZoneInfo(zone)
            return day, zone
        except (KeyError, ValueError, OSError):
            continue
    return None


async def is_run_cancelled(response_id: UUID, redis: Redis) -> bool:
    """Check whether a cancellation request has been registered in Redis for this response.

    Args:
        response_id: The UUID of the active response run.
        redis: Connected asynchronous Redis client.

    Returns:
        True if the cancellation key exists, False otherwise.
    """
    key = f"{CANCEL_KEY_PREFIX}{response_id}"
    try:
        return bool(await redis.exists(key))
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Failed to check Redis cancellation flag: %s", exc)
        return False


async def is_history_storage_enabled(
    session: AsyncSession,
    scope: Scope | None = None,
    *,
    owner_id: int = 1,
) -> bool:
    """Read the owner history choice while serializing absent-row initialization and updates.

    Args:
        session: Active asynchronous database session.
        scope: The caller's workspace scope; None falls back to ``owner_id``'s default workspace.
        owner_id: Account whose default workspace is used when ``scope`` is None.

    Returns:
        True when durable history is permitted; False when new history is temporary.

    Side effects:
        Holds Memory's owner privacy advisory lock until the caller commits or rolls back.
    """
    resolved = scope or await owner_default_scope(session, owner_id)
    await lock_export_privacy(session, resolved.workspace_id, shared=True)
    return (
        await read_export_privacy(
            session,
            scope=resolved,
            multi_workspace_enabled=multi_workspace_enabled(),
        )
    ).store_conversation_history


async def _require_privacy_fence(
    session: AsyncSession, expected: object, *, scope: Scope | None
) -> bool:
    """Lock and compare the public Memory consent version against response admission.

    Args:
        session: Transaction that will retain the Memory advisory lock through publication.
        expected: Serialized value, persisted marker, and updated timestamp stamped by Chat.
        scope: The run actor's workspace scope; None (actor no longer admitted) fails the fence.

    Returns:
        The captured boolean value when the strict snapshot still matches.

    Raises:
        PrivacyFenceChanged: The stamp is missing, malformed, changed, or unavailable.
    """
    if scope is None:
        raise PrivacyFenceChanged("History consent is unavailable")
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    try:
        current = await read_export_privacy(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled()
        )
    except Exception as exc:
        raise PrivacyFenceChanged("History consent is unavailable") from exc
    if not isinstance(expected, dict):
        raise PrivacyFenceChanged("History consent fence is missing")
    expected_enabled = expected.get("store_conversation_history")
    expected_persisted = expected.get("persisted")
    expected_time = expected.get("updated_at")
    actual_time = current.updated_at.isoformat() if current.updated_at is not None else None
    if (
        type(expected_enabled) is not bool
        or type(expected_persisted) is not bool
        or expected_enabled != current.store_conversation_history
        or expected_persisted != current.persisted
        or expected_time != actual_time
    ):
        raise PrivacyFenceChanged("History consent changed after response admission")
    return current.store_conversation_history


async def _lock_live_response(
    session: AsyncSession,
    response_id: UUID,
    conversation_id: UUID,
    expected_fence: object,
    *,
    scope: InternalJobScope | None,
    access_fence: AccessFence | None,
) -> tuple[bool, ResponseRun]:
    """Lock Memory consent, live conversation, then run through a sensitive transaction.

    Args:
        session: Transaction kept open until the guarded read or publication commits.
        response_id: Run whose pending status and sequence must be serialized.
        conversation_id: Parent whose existence and ephemeral deadline gate publication.
        expected_fence: Memory consent snapshot attached at request admission.
        scope: Job subject admitted at claim.
        access_fence: Account/workspace/membership snapshot taken at claim (D6), compared under lock.

    Returns:
        Current history choice and the locked active ResponseRun.

    Raises:
        PrivacyFenceChanged: Consent changed/unavailable or the parent expired/disappeared.
        ResponseNoLongerActive: A serialized Stop or completion already won the run lock.
    """
    if scope is None or access_fence is None:
        raise PrivacyFenceChanged("Chat job was not admitted")
    try:
        await workspaces.lock_access_fence(
            session,
            scope=scope,
            expected=access_fence,
            multi_workspace_enabled=multi_workspace_enabled(),
        )
    except (HTTPException, DomainError) as exc:
        raise PrivacyFenceChanged("Chat access changed after response admission") from exc
    history_enabled = await _require_privacy_fence(session, expected_fence, scope=scope)
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    run = await session.scalar(
        select(ResponseRun)
        .where(ResponseRun.id == response_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None or run is None:
        raise PrivacyFenceChanged("Conversation or response no longer exists")
    if conversation.ephemeral and (
        conversation.expires_at is None or conversation.expires_at <= datetime.now(UTC)
    ):
        raise PrivacyFenceChanged("Temporary conversation has expired")
    if run.status != "streaming":
        raise ResponseNoLongerActive("Response was cancelled or completed before publication")
    return history_enabled, run


async def _next_event_seq(session: AsyncSession, response_id: UUID, current_seq: int) -> int:
    """Allocate the next event identity while the caller holds the response row lock.

    Args:
        session: Transaction holding the response row lock.
        response_id: Run receiving the next event.
        current_seq: Local worker high-water mark, which may lag an external terminal write.

    Returns:
        A sequence strictly greater than both the persisted and local high-water marks.
    """
    latest = await session.scalar(
        select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
            StreamEvent.response_id == response_id,
        )
    )
    return max(current_seq, latest or 0) + 1
