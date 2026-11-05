"""Chat worker run state: per-run context dataclasses, live-run fencing, SSE chunk parsing and the run claim."""

import asyncio
import contextlib
import enum
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.chat._worker_base import (
    _CLAIM_SEQ,
    SHUTDOWN_RELEASE_TIMEOUT,
    PrivacyFenceChanged,
    ResponseNoLongerActive,
    _lock_live_response,
    is_run_cancelled,
    logger,
)
from modules.chat._worker_settle import (
    _cancel_response_locked,
    _privacy_cancel_locked,
    _release_on_shutdown,
)
from modules.chat._worker_web import (
    _publish_tool_status,
    _WebSearchRun,
)
from modules.chat.generative_ui import (
    ToolCallAccumulator,
)
from modules.chat.models import (
    ResponseRun,
    StreamEvent,
)
from modules.chat.retrieval import (
    revalidate_context_fence,
)
from modules.chat.schemas import (
    AnswerContext,
    AnswerContextRequest,
)
from modules.chat.stream import make_event_id


class _RunStopped(ResponseNoLongerActive):
    """A generation phase ended the run: the terminal state (Stop or privacy redaction) is already persisted.

    Subclasses ``ResponseNoLongerActive`` so the outer handler in ``run_response_generation`` returns silently;
    helpers raise it where the original straight-line code used ``return``.
    """


class _FenceOutcome(enum.Enum):
    """Result of a live-run fence: still live, honoured a Stop, or redacted after an evidence/consent change."""

    LIVE = "live"
    CANCELLED = "cancelled"
    REDACTED = "redacted"


@dataclass
class _RunCtx:
    """Identity of one claimed run plus the mutable worker-side high-water mark ``seq``.

    ``seq`` is the local stream-event sequence watermark; every publish site updates it explicitly through
    ``_next_event_seq`` so it stays strictly increasing even when an external terminal write races.
    ``job_scope``/``access_fence`` are None until admission succeeds; ``answer_context`` is set once
    retrieval completes and arms the evidence revalidation of later fences.
    """

    response_id: UUID
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    redis: Redis
    conversation_id: UUID
    privacy_fence: object
    seq: int
    job_scope: InternalJobScope | None = None
    access_fence: AccessFence | None = None
    answer_context: AnswerContext | None = None
    selected_only: bool = False
    # Event-loop time of the last full evidence revalidation (None: none yet). Drives the flush budget.
    last_full_revalidation: float | None = None

    @property
    def scope(self) -> InternalJobScope:
        """The admitted job scope; only valid after admission (raises PrivacyFenceChanged otherwise)."""
        if self.job_scope is None:
            raise PrivacyFenceChanged("Chat job was not admitted")
        return self.job_scope

    @property
    def access(self) -> AccessFence:
        """The admitted access fence; only valid after admission (raises PrivacyFenceChanged otherwise)."""
        if self.access_fence is None:
            raise PrivacyFenceChanged("Chat job was not admitted")
        return self.access_fence


@dataclass(frozen=True)
class _ClaimedRun:
    """Columns returned by the atomic pending -> streaming claim."""

    conversation_id: UUID
    user_message_id: UUID
    context_req: dict[str, Any] | None
    ephemeral: bool
    workspace_id: UUID
    actor_id: int


@dataclass
class _TurnInputs:
    """Everything read before retrieval (prompt, history, request) plus the retrieval/web results."""

    user_prompt: str
    revision_of_message_id: UUID | None
    prior_messages: list[dict[str, str]]
    req_params: dict[str, Any]
    day_scope: tuple[str, str] | None
    answer_request: AnswerContextRequest
    answer_context: AnswerContext | None = None
    web_run: _WebSearchRun | None = None


@dataclass
class _StreamState:
    """Worker-memory stream buffers: text/reasoning/UI snapshots not yet published, and what was published.

    ``pending*`` are flushed by ``_flush_pending`` (one locked transaction); ``accumulated_*`` only ever
    contain flushed text, so the persisted answer equals what clients saw.
    """

    pending: str = ""
    pending_reasoning: str = ""
    pending_ui: dict[str, dict[str, Any]] = field(default_factory=dict)
    accumulated_text: str = ""
    accumulated_reasoning: str = ""  # published reasoning, capped at REASONING_MAX_CHARS
    reasoning_truncated: bool = False
    last_flush: float = float("-inf")

    def drop_pending(self) -> None:
        """Discard unpublished buffers after the run was cancelled or redacted."""
        self.pending = ""
        self.pending_reasoning = ""
        self.pending_ui = {}


@dataclass
class _RoundState:
    """Per-model-round streaming state (reset for every tool-continuation round)."""

    acc: ToolCallAccumulator = field(default_factory=ToolCallAccumulator)
    # Provider thinking blocks (Anthropic) of this round: must ride back with the tool results or the next round 400s.
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    started: bool = False  # any chunk received; a capability fallback is only safe before this
    ui_state: dict[str, dict[str, Any]] = field(default_factory=dict)  # per part id: length, milestones, parse clock, snapshot
    text_start: int = 0


@dataclass(frozen=True)
class _SseChunk:
    """The fields of one streamed ``data:`` chunk that the worker acts on."""

    content: Any
    reasoning: Any
    details: Any
    tool_fragments: Any


def _parse_sse_chunk(payload: str) -> _SseChunk | None:
    """Decode one SSE ``data:`` payload into the worker-relevant delta fields (pure).

    Returns None when the chunk has no ``choices`` list. Raises ``json.JSONDecodeError`` for invalid JSON and
    ``AttributeError`` for structurally wrong chunks (non-dict choice/delta); the stream loop ignores both.
    The gateway folds provider ``reasoning`` and inline ``<think>`` blocks into ``reasoning_content``;
    ``reasoning`` is still read for gateways that skip that.
    """
    chunk_obj = json.loads(payload)
    choices = chunk_obj.get("choices", [])
    if not (choices and isinstance(choices, list)):
        return None
    delta = choices[0].get("delta", {})
    return _SseChunk(
        content=delta.get("content", ""),
        reasoning=delta.get("reasoning_content") or delta.get("reasoning"),
        details=delta.get("reasoning_details"),
        tool_fragments=delta.get("tool_calls"),
    )


async def _fence_live_run(
    session: AsyncSession, c: _RunCtx, *, check_evidence: bool, budgeted: bool = False,
) -> tuple[_FenceOutcome, ResponseRun]:
    """Lock the live run (privacy -> conversation -> run), honour Stop, optionally revalidate evidence.

    Shared by every mid-run publish site. A Stop persists the ordinary cancelled terminal state and commits;
    drifted evidence (``check_evidence`` and retrieved evidence present) persists the redacted terminal state
    and commits. In both cases the outcome is not ``LIVE`` and the caller must stop publishing. On ``LIVE``
    the caller's transaction still holds every lock. Raises PrivacyFenceChanged / ResponseNoLongerActive from
    ``_lock_live_response``. ``budgeted`` (token flushes only) lets ``_evidence_still_valid`` use the
    lightweight lock-only revalidation between full ones.
    """
    _, live_run = await _lock_live_response(
        session, c.response_id, c.conversation_id, c.privacy_fence, scope=c.job_scope, access_fence=c.access_fence,
    )
    if await is_run_cancelled(c.response_id, c.redis):
        await _cancel_response_locked(session, live_run, c.seq)
        await session.commit()
        return _FenceOutcome.CANCELLED, live_run
    if check_evidence and not await _evidence_still_valid(session, c, live_run, budgeted=budgeted):
        return _FenceOutcome.REDACTED, live_run
    return _FenceOutcome.LIVE, live_run


def _full_revalidation_due(c: _RunCtx, now: float) -> bool:
    """Whether this flush must run the full evidence revalidation (first one, or the budget elapsed).

    ``Settings.chat_flush_full_revalidate_seconds`` bounds how long the heavy checks (separate
    exact-selection pass and content-row reads) may be skipped; 0 disables the budget (always full).
    The lightweight path still locks and compares the cited chunks and source state on every flush,
    so a consent/privacy revocation, source deactivation, generation bump, local-only flip or deletion
    stops publication on the very next flush; only the extra heavy checks wait at most this budget.
    """
    interval = float(getattr(c.settings, "chat_flush_full_revalidate_seconds", 0.0) or 0.0)
    return interval <= 0.0 or c.last_full_revalidation is None or now - c.last_full_revalidation >= interval


async def _evidence_still_valid(
    session: AsyncSession, c: _RunCtx, live_run: ResponseRun, *, budgeted: bool = False,
) -> bool:
    """Revalidate (and lock) every cited evidence reference; on drift redact the run and commit.

    Returns True when there is no evidence or every reference is current. Must run inside the live-run fence.
    With ``budgeted`` (token flushes) the full revalidation runs at most every
    ``chat_flush_full_revalidate_seconds``; in between a lightweight pass takes the same locks and compares
    the same generation/status/local-only/existence state without reading content rows.
    """
    answer_context = c.answer_context
    if answer_context is None or not answer_context.evidence:
        return True
    now = asyncio.get_running_loop().time()
    lightweight = budgeted and not _full_revalidation_due(c, now)
    fences_ok, _fence_reasons = await revalidate_context_fence(
        session, answer_context, destination="remote",
        require_current_versions=c.selected_only, lock_evidence=True, lightweight=lightweight, scope=c.scope,
    )
    if fences_ok and not lightweight:
        c.last_full_revalidation = now
    if fences_ok:
        return True
    await _privacy_cancel_locked(session, live_run, c.seq)
    await session.commit()
    return False


@contextlib.asynccontextmanager
async def _live_run_fence(
    c: _RunCtx, *, check_evidence: bool, budgeted: bool = False,
) -> AsyncIterator[tuple[AsyncSession, _FenceOutcome, ResponseRun]]:
    """Open a session and yield it with the live-run fence outcome (see ``_fence_live_run``).

    The session (and, on ``LIVE``, every lock) is released when the block exits; callers publish and commit
    inside the block only when the outcome is ``LIVE``.
    """
    async with c.session_factory() as session:
        outcome, live_run = await _fence_live_run(session, c, check_evidence=check_evidence, budgeted=budgeted)
        yield session, outcome, live_run


def _add_event(session: AsyncSession, c: _RunCtx, seq: int, event_type: str, data: dict[str, Any]) -> None:
    """Stage one ``StreamEvent`` row for this run at an already allocated ``seq``."""
    session.add(StreamEvent(
        response_id=c.response_id, seq=seq, event_type=event_type,
        event_id=make_event_id(c.response_id, seq), data=data,
    ))


async def _publish_status(c: _RunCtx, tool: str, state: str) -> None:
    """Publish a ``tool.status`` event through ``_publish_tool_status`` and advance ``c.seq``."""
    c.seq = await _publish_tool_status(
        c.session_factory, c.response_id, c.conversation_id, c.privacy_fence, c.seq, tool, state,
        scope=c.scope, access_fence=c.access,
    )


async def _claim_run(
    response_id: UUID, session_factory: async_sessionmaker[AsyncSession],
) -> _ClaimedRun | None:
    """Atomically move a pending run to streaming and publish its first ``status`` event (seq 1).

    Returns None when the run was already claimed or cancelled. The claim commit happens before any
    admission or I/O. A cancellation around the claim commit hands the run back (``_release_on_shutdown``,
    a no-op unless it is ``streaming``) instead of waiting RECOVER_STREAMING_AFTER, then re-raises.
    """
    try:
        async with session_factory() as session:
            result = await session.execute(
                update(ResponseRun)
                .where(ResponseRun.id == response_id, ResponseRun.status == "pending")
                .values(status="streaming")
                .returning(
                    ResponseRun.conversation_id, ResponseRun.user_message_id,
                    ResponseRun.retrieval_context, ResponseRun.ephemeral,
                    ResponseRun.workspace_id, ResponseRun.actor_user_id,
                )
            )
            claimed = result.fetchone()
            if claimed is None:
                # Run was already claimed or cancelled before start
                return None
            row = cast(
                "tuple[UUID, UUID, dict[str, Any] | None, bool, UUID, int]", tuple(claimed),
            )
            session.add(StreamEvent(
                response_id=response_id, seq=_CLAIM_SEQ, event_type="status",
                event_id=make_event_id(response_id, _CLAIM_SEQ), data={"status": "streaming"},
            ))
            await session.commit()
    except asyncio.CancelledError:
        # Cancelled around the claim commit: the run may be `streaming` with nothing published. Hand it back
        # now (no-op unless it is `streaming`) instead of waiting RECOVER_STREAMING_AFTER.
        try:
            await asyncio.wait_for(_release_on_shutdown(response_id, session_factory, None), SHUTDOWN_RELEASE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001  # boundary: recovery is the safety net
            logger.warning("Chat run claim-cancel release failed for %s (%s)", response_id, type(exc).__name__)
        raise
    return _ClaimedRun(*row)
