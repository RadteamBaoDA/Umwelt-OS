"""Background generation worker and ARQ task handler for chat model generation.

Entry points (``process_chat_response``, ``recover_chat_runs``, ``run_response_generation``) live here; the
implementation is split across the private ``modules.chat._worker_*`` modules, whose names are re-exported
below so ``modules.chat.worker`` keeps its original surface.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.client import (
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.worker_context import worker_session_factory
from core.workspaces import public as workspaces  # noqa: F401
from modules.chat import (
    media,  # noqa: F401
    signals,
)
from modules.chat import web_search as web  # noqa: F401

# Re-exports: ``modules.chat.worker`` keeps its original public and test-visible surface.
from modules.chat._worker_base import (  # noqa: F401
    _CHAT_ROLE,
    _CLAIM_SEQ,
    _UI_MILESTONE_KEYS,
    _WEB_UNAVAILABLE_CODES,
    CANCEL_CHECK_INTERVAL,
    DELTA_PURGE_LIMIT,
    DELTA_RETENTION,
    EPHEMERAL_TTL,
    MAX_SKILL_CALLS_PER_ROUND,
    MAX_SKILL_ROUNDS,
    MAX_TRIP_IMAGE_CALLS_PER_ROUND,
    MAX_USER_INPUT_REQUESTS_PER_TURN,
    REASONING_MAX_CHARS,
    RECOVER_PENDING_AFTER,
    RECOVER_PENDING_MAX_AGE,
    RECOVER_STREAMING_AFTER,
    SHUTDOWN_RELEASE_TIMEOUT,
    SHUTDOWN_TIMEOUT_MARGIN,
    STREAM_FLUSH_CHARS,
    STREAM_FLUSH_SECONDS,
    WEB_SEARCH_FENCE_SECONDS,
    WEB_SEARCH_NO_RESULTS_PROMPT,
    WEB_SEARCH_STOP_SECONDS,
    WEB_SEARCH_UNAVAILABLE_PROMPT,
    PrivacyFenceChanged,
    ResponseNoLongerActive,
    _admit_job,
    _day_scope,
    _job_scope,
    _lock_live_response,
    _next_event_seq,
    _scope_for_run,
    is_run_cancelled,
    logger,
)
from modules.chat._worker_base import (
    CANCEL_KEY_PREFIX as CANCEL_KEY_PREFIX,
)
from modules.chat._worker_base import (
    CHAT_JOB_TIMEOUT as CHAT_JOB_TIMEOUT,
)
from modules.chat._worker_base import (
    CHAT_QUEUE as CHAT_QUEUE,
)
from modules.chat._worker_base import (
    _require_privacy_fence as _require_privacy_fence,
)
from modules.chat._worker_base import (
    is_history_storage_enabled as is_history_storage_enabled,
)
from modules.chat._worker_finalize import (  # noqa: F401
    _assistant_metadata,
    _CitedAnswer,
    _finalize_widgets,
    _persist_final,
    _tool_citation_numbers,
    _validate_citations,
    _validate_document_citations,
    _web_citations_for,
)
from modules.chat._worker_purge import (  # noqa: F401
    _purge_expired_activity_links,
    _purge_expired_conversations,
    _purge_expired_runs,
    purge_terminal_message_deltas,
)
from modules.chat._worker_purge import (
    purge_expired_chat_runs as purge_expired_chat_runs,
)
from modules.chat._worker_settle import (
    _cancel_response_locked as _cancel_response_locked,
)
from modules.chat._worker_settle import (
    _mark_failed,
    _release_on_shutdown,
)
from modules.chat._worker_settle import (
    _mark_privacy_cancelled as _mark_privacy_cancelled,
)
from modules.chat._worker_settle import (
    _privacy_cancel_locked as _privacy_cancel_locked,
)
from modules.chat._worker_settle import (
    neutralize_ui_events as neutralize_ui_events,
)
from modules.chat._worker_settle import (
    purge_run_model_copies as purge_run_model_copies,
)
from modules.chat._worker_setup import (  # noqa: F401
    _build_gateway,
    _build_model_setup,
    _compose_system_text,
    _external_route_decision,
    _gadget_note,
    _history_cutoff,
    _load_turn_inputs,
    _ModelSetup,
    _owner_locale,
    _prepare_turn,
    _resolve_ai_setup,
    _retrieve_with_web,
    _route_visualization,
    _SendAttemptFence,
    _web_prompt_suffix,
)
from modules.chat._worker_state import (  # noqa: F401
    _add_event,
    _claim_run,
    _ClaimedRun,
    _evidence_still_valid,
    _fence_live_run,
    _FenceOutcome,
    _full_revalidation_due,
    _live_run_fence,
    _parse_sse_chunk,
    _publish_status,
    _RoundState,
    _RunCtx,
    _RunStopped,
    _SseChunk,
    _StreamState,
    _TurnInputs,
)
from modules.chat._worker_stream import (  # noqa: F401
    _answer_tool_calls,
    _apply_sse_line,
    _apply_text_delta,
    _apply_tool_fragments,
    _bounded_continue,
    _classify_round,
    _consume_round,
    _finish_round,
    _round_terminals,
    _RoundCalls,
    _stream_turn,
)
from modules.chat._worker_tools import (  # noqa: F401
    _TOOL_HANDLERS,
    ChatToolHandler,
    _flush_pending,
    _LocalDataHandler,
    _park_for_input,
    _publish_ui_discards,
    _QueryCollectedDataHandler,
    _ReadSkillHandler,
    _RequestUserInputHandler,
    _run_todos,
    _ToolEnv,
    _TripImagesHandler,
    _TurnState,
    register_chat_tool,
)
from modules.chat._worker_web import (  # noqa: F401
    REWRITE_INSTRUCTIONS,
    REWRITE_MESSAGE_CHARS,
    REWRITE_QUERY_CHARS,
    REWRITE_TIMEOUT_SECONDS,
    REWRITE_USER_MESSAGES,
    _gate_search_attempt,
    _publish_tool_status,
    _publish_web_search,
    _rewrite_and_search,
    _rewrite_web_query,
    _search_for_run,
    _search_targets,
    _SearchArgs,
    _SearchAttempt,
    _SearchProgress,
    _send_search,
    _stop_search,
    _web_search_run,
    _WebSearchRun,
)
from modules.chat.db_query import TOOL_QUERY_COLLECTED_DATA  # noqa: F401
from modules.chat.generative_ui import TOOL_READ_SKILL, TOOL_TRIP_IMAGES  # noqa: F401
from modules.chat.generative_ui import external_router as viz  # noqa: F401
from modules.chat.generative_ui.tool_specs import (  # noqa: F401
    LOCAL_DATA_TOOLS,
    TOOL_REQUEST_USER_INPUT,
)
from modules.chat.models import (
    Conversation,
    ResponseRun,
    StreamEvent,
)
from modules.chat.schemas import (
    AnswerContext,
)
from modules.chat.stream import make_event_id
from modules.memory.public import lock_export_privacy
from modules.settings import public as settings_public  # noqa: F401
from modules.sources import public as sources_public  # noqa: F401


async def _generate(c: _RunCtx, claimed: _ClaimedRun) -> None:
    """Run the generation phases for a claimed run: prepare, model setup, stream, validate, persist.

    Raises ``_RunStopped`` (a ``ResponseNoLongerActive``) whenever a phase persisted a terminal state; every
    other failure propagates to ``run_response_generation``'s handlers.
    """
    turn = await _prepare_turn(c, claimed)
    setup = await _build_model_setup(c, claimed, turn)
    # 3b. Opt-in external visualization routing (see _route_visualization); None leaves the request byte-identical.
    route_decision = await _route_visualization(c, turn, setup)
    messages_payload = setup.base_messages(True)
    ts = _TurnState(
        messages_payload=messages_payload, stream_iter=setup.open_stream(messages_payload, setup.ui_tools),
        tools_active=setup.ui_tools is not None,
        max_parts=max(1, int(getattr(c.settings, "generative_ui_max_parts", 4))),
        max_ui_rounds=max(1, int(getattr(c.settings, "generative_ui_max_rounds", 3))),
        input_ttl_seconds=int(getattr(c.settings, "chat_input_request_ttl_seconds", 900)),
        last_cancel_check=asyncio.get_running_loop().time(), route_decision=route_decision,
    )
    st = _StreamState()
    env = _ToolEnv(c, claimed, turn, setup, ts)
    await _stream_turn(env, st)
    # 5. Complete generation and citation validation
    cited = _validate_citations(
        st.accumulated_text, cast(AnswerContext, turn.answer_context), turn.answer_request, turn.web_run, ts.ui_parts,
    )
    generative_uis = _finalize_widgets(env, cited.final_answer, cited)
    await _persist_final(env, st, cited.final_answer, cited.valid_citations, generative_uis)


async def run_response_generation(
    response_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
) -> None:
    """Generate a grounded response (see ``_run_response_generation``) and wake its SSE streams per commit.

    While the run executes, every commit that inserted ``StreamEvent`` rows publishes a best-effort
    ``chat:response:<id>`` Redis message AFTER the commit (``modules.chat.signals``). The message only
    shortens SSE latency; the stream still reads events from the database in ``seq`` order and polls
    slowly as a fallback, so a lost message changes nothing but timing.
    """
    with signals.signal_run_commits(response_id, redis):
        await _run_response_generation(response_id, session_factory, settings, redis)


async def _run_response_generation(
    response_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
) -> None:
    """Generate a grounded response while current evidence is locked at every external boundary.

    Manages the lifecycle of a single ResponseRun: transitions status from pending to streaming,
    retrieves grounded context, fences rerank/chat egress and every citation/delta/final publication,
    validates citations, persists the completed assistant message and terminal event, and handles
    deletion-driven cancellation under the Memory/Chat parent/run lock order.

    The claim commits before admission. Phases run inside one handler whose clause order is part of the
    contract: PrivacyFenceChanged/PrivacyPolicyDenied (redact), ResponseNoLongerActive (terminal state
    already persisted), CancelledError (hand back for recovery, re-raise), then any other Exception (fail).

    Args:
        response_id: Unique UUID of the ResponseRun to process.
        session_factory: Database async session factory for creating isolated transactions.
        settings: Application settings containing gateway and network configuration.
        redis: Asynchronous Redis client for cancellation tracking and gateway leasing.

    Raises:
        None: All operational errors are caught, logged without sensitive content, and persisted.
    """
    claimed = await _claim_run(response_id, session_factory)
    if claimed is None:
        return
    claimed_at = time.monotonic()
    raw_context_req = claimed.context_req
    privacy_fence = (raw_context_req or {}).get("_chat_privacy_fence") if isinstance(raw_context_req, dict) else None
    c = _RunCtx(
        response_id=response_id, session_factory=session_factory, settings=settings, redis=redis,
        conversation_id=claimed.conversation_id, privacy_fence=privacy_fence, seq=_CLAIM_SEQ,
    )
    try:
        await _generate(c, claimed)
    except (PrivacyFenceChanged, PrivacyPolicyDenied):
        logger.info("Privacy fence cancelled chat response %s", response_id)
        await _mark_privacy_cancelled(response_id, session_factory, c.seq)
    except ResponseNoLongerActive:
        return
    except asyncio.CancelledError:
        # arq cancels in-flight jobs on SIGTERM/deploy. Make the run recoverable now instead of after
        # RECOVER_STREAMING_AFTER; bounded, never raises, and the cancellation still propagates.
        # arq's job_timeout cancels the same way; near the timeout it is not a shutdown, so fail (no retry loop).
        timed_out = time.monotonic() - claimed_at >= CHAT_JOB_TIMEOUT - SHUTDOWN_TIMEOUT_MARGIN
        try:
            await asyncio.wait_for(
                _release_on_shutdown(response_id, session_factory, privacy_fence, timed_out, scope=c.job_scope),
                SHUTDOWN_RELEASE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001  # boundary: recovery's abandonment threshold is the safety net
            logger.warning("Chat run shutdown release failed for %s (%s)", response_id, type(exc).__name__)
        raise
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        # Router failures carry a safe code (never a URL, body or key); logging it names the setting to fix.
        # ModelGatewayError messages are built by the gateway (HTTP status, capability, policy, and a
        # secret-redacted, length-capped upstream reason), so they are safe to log and say why the model
        # failed; other exception text may carry payloads, so it stays out of the log.
        code = getattr(exc, "code", None)
        reason = f" [{exc}]" if isinstance(exc, ModelGatewayError) else ""
        logger.error("Response generation failed for run %s: %s%s%s", response_id, type(exc).__name__, f" ({code})" if code else "", reason)
        await _mark_failed(response_id, session_factory, c.seq, privacy_fence, exc, scope=c.job_scope)


async def process_chat_response(ctx: dict[str, object], response_id: str) -> None:
    """ARQ task entry point to process a chat response generation run.

    Args:
        ctx: ARQ worker context dict containing settings, session_factory, and redis.
        response_id: String UUID of the ResponseRun to execute.
    """
    settings = cast(Settings, ctx["settings"])
    session_factory = worker_session_factory(ctx)
    redis = cast(Redis, ctx.get("redis"))
    await run_response_generation(UUID(response_id), session_factory, settings, redis)


async def _fail_expired_pending(response_id: UUID, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Fail a run that never started (nothing published); privacy -> conversation -> run lock order."""
    async with session_factory() as session:
        hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if hint is None:
            return
        # Reader: terminal publication only needs a stable consent read (shared, per workspace).
        # The workspace is immutable on the run, so this lock-free hint read may precede the lock.
        await lock_export_privacy(session, hint.workspace_id, shared=True)
        await session.scalar(select(Conversation).where(
            Conversation.id == hint.conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == response_id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status != "pending" or run.updated_at >= datetime.now(UTC) - RECOVER_PENDING_MAX_AGE:
            return  # released back to pending (fresh updated_at) between select and lock: re-enqueue instead
        run.status = "failed"
        run.error_code = "TimeoutError"
        run.error_message = "chat worker unavailable"
        run.completed_at = func.now()
        seq = await _next_event_seq(session, response_id, 0)
        session.add(StreamEvent(
            response_id=response_id, seq=seq, event_type="status",
            event_id=make_event_id(response_id, seq),
            data={"status": "failed", "error": "chat worker unavailable"},
        ))
        await session.commit()


async def recover_chat_runs(ctx: dict[str, object]) -> dict[str, int]:
    """Re-enqueue stuck pending runs and fail streaming runs whose generator is gone.

    Re-enqueue is safe: the pending->streaming claim is atomic and the arq job id is fixed. Abandoned
    runs go through `_mark_failed`, which keeps the privacy -> conversation -> run lock order and the
    privacy-redaction fallback. Pending runs untouched for RECOVER_PENDING_MAX_AGE (by `updated_at`, which
    a release-to-pending refreshes) are failed instead of re-enqueued.
    """
    factory = worker_session_factory(ctx)
    redis = cast(Any, ctx["redis"])
    now = datetime.now(UTC)
    async with factory() as session:
        pending_ids = list(await session.scalars(
            select(ResponseRun.id)
            .where(
                ResponseRun.status == "pending",
                ResponseRun.created_at < now - RECOVER_PENDING_AFTER,
                ResponseRun.updated_at >= now - RECOVER_PENDING_MAX_AGE,
            )
            .order_by(ResponseRun.created_at).limit(50)
        ))
        expired = list(await session.scalars(
            select(ResponseRun.id)
            .where(ResponseRun.status == "pending", ResponseRun.updated_at < now - RECOVER_PENDING_MAX_AGE)
            .order_by(ResponseRun.created_at).limit(50)
        ))
        last_event = (
            select(func.max(StreamEvent.created_at))
            .where(StreamEvent.response_id == ResponseRun.id).scalar_subquery()
        )
        cutoff = now - RECOVER_STREAMING_AFTER
        stale = (await session.execute(
            select(ResponseRun.id, ResponseRun.retrieval_context, ResponseRun.workspace_id, ResponseRun.actor_user_id)
            .where(
                ResponseRun.status == "streaming",
                ResponseRun.updated_at < cutoff,
                func.coalesce(last_event, ResponseRun.updated_at) < cutoff,
            )
            .order_by(ResponseRun.updated_at).limit(50)
        )).all()
    for expired_id in expired:
        await _fail_expired_pending(expired_id, factory)
    for run_id in pending_ids:
        try:
            await redis.enqueue_job(
                "process_chat_response", str(run_id),
                _job_id=f"chat-response:{run_id}", _queue_name=CHAT_QUEUE,
            )
        except Exception as exc:  # noqa: BLE001  # boundary: next poll retries
            logger.warning("Chat run re-enqueue failed for %s (%s)", run_id, type(exc).__name__)
    for stale_id, context, stale_workspace, stale_actor in stale:
        fence = context.get("_chat_privacy_fence") if isinstance(context, dict) else None
        async with factory() as session:
            seq = await session.scalar(select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
                StreamEvent.response_id == stale_id,
            )) or 0
        await _mark_failed(
            cast(UUID, stale_id), factory, seq, fence, TimeoutError("generation abandoned"),
            scope=await _scope_for_run(factory, cast(UUID, stale_workspace), cast(int, stale_actor)),
        )
    return {"requeued": len(pending_ids), "failed": len(stale) + len(expired)}
