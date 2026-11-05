"""Chat worker settlement: cancel, privacy-cancel and failure marking, shutdown release and run model-copy purge."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.model_gateway.client import (
    ModelGatewayError,
)
from core.workspaces.schemas import InternalJobScope
from modules.chat._worker_base import (
    EPHEMERAL_TTL,
    _next_event_seq,
    _require_privacy_fence,
)
from modules.chat.models import (
    ChatTemplate,
    ChatTodo,
    Conversation,
    ResponseRun,
    StreamEvent,
)
from modules.chat.stream import make_event_id
from modules.memory.public import lock_export_privacy


async def _release_on_shutdown(
    response_id: UUID, session_factory: async_sessionmaker[AsyncSession], expected_fence: object, timed_out: bool = False,
    *, scope: InternalJobScope | None = None,
) -> None:
    """Worker is stopping: hand a streaming run back as pending, or fail it truthfully if content was published.

    Same privacy -> conversation -> run lock order as `_mark_failed`. With no delta/citation published the
    client saw nothing but the `streaming` status, so the run returns to `pending` (its events are dropped so
    the re-claim can reuse seq 1) and `recover_chat_runs` re-enqueues it; otherwise it fails like an
    abandoned run.
    """
    async with session_factory() as session:
        run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run_hint is None:
            return
        # Reader: terminal publication only needs a stable consent read (shared, per workspace).
        # The workspace is immutable on the run, so this lock-free hint read may precede the lock.
        await lock_export_privacy(session, run_hint.workspace_id, shared=True)
        await session.scalar(select(Conversation).where(
            Conversation.id == run_hint.conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == response_id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status != "streaming":
            return
        published = await session.scalar(select(StreamEvent.id).where(
            StreamEvent.response_id == response_id,
            StreamEvent.event_type.in_(("message.delta", "message.reasoning.delta", "message.citations")),
        ).limit(1))
        if published is None and not timed_out:
            await session.execute(delete(StreamEvent).where(StreamEvent.response_id == response_id))
            run.status = "pending"
            await session.commit()
            return
        seq = await session.scalar(select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
            StreamEvent.response_id == response_id,
        )) or 0
        await session.rollback()
    await _mark_failed(response_id, session_factory, seq, expected_fence, TimeoutError("generation timed out" if timed_out else "chat worker shutting down"), scope=scope)


async def neutralize_ui_events(session: AsyncSession, response_id: UUID) -> None:
    """Blank every persisted ``message.ui.*`` event payload of one run.

    Streaming deltas carry no document identifiers, so privacy cleanup and cancellation neutralize
    all widget events of a run together. Event rows and seqs stay so replay keeps its identities.
    """
    await session.execute(update(StreamEvent).where(
        StreamEvent.response_id == response_id,
        StreamEvent.event_type.in_(("message.ui.delta", "message.ui.done", "message.ui.discard")),
    ).values(data={}))


async def purge_run_model_copies(session: AsyncSession, response_id: UUID) -> None:
    """Drop every model-derived copy of one run outside its widgets: input request, input events, todos, templates.

    Clears the run's ``input_request`` (prompt, options, free-text answer), blanks the run's ``message.input.*`` event
    payloads (rows and seqs stay for replay identity), and deletes ``chat_todos`` and model-created
    ``chat_templates`` whose ``source_response_id`` is this run. Owner-created templates have no provenance and stay.
    Works for any run status; the caller holds the run lock and commits.
    """
    await session.execute(update(ResponseRun).where(ResponseRun.id == response_id).values(input_request=None)
                          .execution_options(synchronize_session="fetch"))
    await session.execute(update(StreamEvent).where(
        StreamEvent.response_id == response_id,
        StreamEvent.event_type.in_(("message.input.request", "message.input.resolved")),
    ).values(data={}))
    await session.execute(delete(ChatTodo).where(ChatTodo.source_response_id == response_id))
    await session.execute(delete(ChatTemplate).where(ChatTemplate.source_response_id == response_id))


async def _privacy_cancel_locked(session: AsyncSession, run: ResponseRun, current_seq: int = 0) -> int:
    """Redact an active run while the caller holds Memory, parent, then run locks.

    Existing event identities stay stable. A previously cancelled Stop is upgraded in place
    without allocating a colliding second terminal event; completed runs are never redacted.
    """
    if run.status not in ("pending", "streaming", "awaiting_input", "cancelled"):
        return current_seq
    await session.execute(update(StreamEvent).where(
        StreamEvent.response_id == run.id,
        StreamEvent.event_type.in_(("message.delta", "message.reasoning.delta")),
    ).values(data={"text": ""}))
    await session.execute(update(StreamEvent).where(
        StreamEvent.response_id == run.id,
        StreamEvent.event_type == "message.citations",
    ).values(data={"citations": []}))
    # Widgets carry model output derived from the withdrawn evidence (tables, sandbox code): neutralize them too.
    await neutralize_ui_events(session, run.id)
    # Parked input request, its events and todos/templates the model wrote from the evidence.
    await purge_run_model_copies(session, run.id)
    was_cancelled = run.status == "cancelled"
    run.status = "cancelled"
    run.ephemeral = True
    run.expires_at = datetime.now(UTC) + EPHEMERAL_TTL
    run.citations = []
    run.completed_at = func.now()
    latest_seq = await session.scalar(select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
        StreamEvent.response_id == run.id,
    ))
    if not was_cancelled:
        terminal_seq = max(current_seq, latest_seq or 0) + 1
        session.add(StreamEvent(
            response_id=run.id,
            seq=terminal_seq,
            event_type="status",
            event_id=make_event_id(run.id, terminal_seq),
            data={"status": "cancelled"},
        ))
        return terminal_seq
    return max(current_seq, latest_seq or 0)


async def _cancel_response_locked(session: AsyncSession, run: ResponseRun, current_seq: int = 0) -> int:
    """Persist ordinary Stop after consent verification with a run-locked terminal sequence."""
    if run.status not in ("pending", "streaming", "awaiting_input"):
        return current_seq
    run.status = "cancelled"
    run.completed_at = func.now()
    latest_seq = await session.scalar(select(func.coalesce(func.max(StreamEvent.seq), 0)).where(
        StreamEvent.response_id == run.id,
    ))
    terminal_seq = max(current_seq, latest_seq or 0) + 1
    session.add(StreamEvent(
        response_id=run.id,
        seq=terminal_seq,
        event_type="status",
        event_id=make_event_id(run.id, terminal_seq),
        data={"status": "cancelled"},
    ))
    return terminal_seq


async def _mark_cancelled(
    response_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    seq: int,
    expected_fence: object,
    *,
    scope: InternalJobScope | None,
) -> None:
    """Honor Stop under consent/parent/run locks and redact when the admitted fence drifted.

    Args:
        response_id: Unique UUID of the ResponseRun being cancelled.
        session_factory: Async database session factory.
        seq: Current stream event sequence number to increment.
    """
    privacy_read_failed = False
    async with session_factory() as session:
        run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run_hint is None:
            return
        # Reader: terminal publication only needs a stable consent read (shared, per workspace).
        # The workspace is immutable on the run, so this lock-free hint read may precede the lock.
        await lock_export_privacy(session, run_hint.workspace_id, shared=True)
        await session.scalar(select(Conversation).where(
            Conversation.id == run_hint.conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == response_id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status not in ("pending", "streaming", "awaiting_input"):
            return
        try:
            await _require_privacy_fence(session, expected_fence, scope=scope)
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # The Memory read can abort this transaction. Drop every held lock before retrying
            # redaction in a clean transaction; otherwise the fallback writes may fail too.
            await session.rollback()
            privacy_read_failed = True
        else:
            await _cancel_response_locked(session, run, seq)
            await session.commit()
    if privacy_read_failed:
        await _mark_privacy_cancelled(response_id, session_factory, seq)


async def _mark_privacy_cancelled(
    response_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    seq: int,
) -> None:
    """Take the canonical lock order and durably redact a response after fence failure."""
    async with session_factory() as session:
        run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run_hint is None:
            return
        # Reader: terminal publication only needs a stable consent read (shared, per workspace).
        # The workspace is immutable on the run, so this lock-free hint read may precede the lock.
        await lock_export_privacy(session, run_hint.workspace_id, shared=True)
        await session.scalar(select(Conversation).where(
            Conversation.id == run_hint.conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == response_id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is not None:
            await _privacy_cancel_locked(session, run, seq)
        await session.commit()


async def _mark_failed(
    response_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    seq: int,
    expected_fence: object,
    exc: Exception,
    *,
    scope: InternalJobScope | None,
) -> None:
    """Serialize terminal failure with consent revocation and Stop; drift takes redaction path.

    Stored/streamed error text is `str(exc)` only for `ModelGatewayError` (vetted, secret-redacted) and the
    worker's own fixed-text `TimeoutError`s ("generation timed out", shutdown, abandoned); any other
    exception yields "Response generation failed". `error_code` is the exception type name.
    """
    privacy_read_failed = False
    async with session_factory() as session:
        run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run_hint is None:
            return
        # Reader: terminal publication only needs a stable consent read (shared, per workspace).
        # The workspace is immutable on the run, so this lock-free hint read may precede the lock.
        await lock_export_privacy(session, run_hint.workspace_id, shared=True)
        await session.scalar(select(Conversation).where(
            Conversation.id == run_hint.conversation_id,
        ).with_for_update().execution_options(populate_existing=True))
        run = await session.scalar(select(ResponseRun).where(
            ResponseRun.id == response_id,
        ).with_for_update().execution_options(populate_existing=True))
        if run is None or run.status != "streaming":
            return
        try:
            await _require_privacy_fence(session, expected_fence, scope=scope)
        except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
            # A failed Memory read may leave PostgreSQL's transaction aborted. Retry the
            # durable redaction only after releasing this transaction and its row locks.
            await session.rollback()
            privacy_read_failed = True
        else:
            run.status = "failed"
            run.error_code = type(exc).__name__
            # Only gateway errors carry vetted (secret-redacted) text; any other exception may embed
            # payloads or paths, so users see a fixed message and the type name stays in the logs.
            # TimeoutError is only raised internally with fixed strings (timeout, shutdown, abandoned run).
            safe_text = str(exc) if isinstance(exc, (ModelGatewayError, TimeoutError)) else "Response generation failed"
            run.error_message = safe_text[:500]
            run.completed_at = func.now()
            terminal_seq = await _next_event_seq(session, response_id, seq)
            session.add(StreamEvent(
                response_id=response_id,
                seq=terminal_seq,
                event_type="status",
                event_id=make_event_id(response_id, terminal_seq),
                data={"status": "failed", "error": safe_text},
            ))
            await session.commit()
    if privacy_read_failed:
        await _mark_privacy_cancelled(response_id, session_factory, seq)
