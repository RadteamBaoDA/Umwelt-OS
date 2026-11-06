"""Response-event SSE polling, framing and heartbeat helpers behind ``modules.chat.routes``."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.chat import signals
from modules.chat._routes.guards import (
    ACTIVE_RUN_STATUSES,
    _auth_row_current,
    _filter_citation_lists,
    _poll_needs_lock,
    _reject_expired_conversation,
    _run_scope,
    _session_is_current,
)
from modules.chat.models import (
    Conversation,
    ResponseRun,
    StreamEvent,
)
from modules.chat.stream import format_sse_event, parse_event_id
from modules.chat.worker import (
    _mark_privacy_cancelled,
    _require_privacy_fence,
)
from modules.memory.public import lock_export_privacy

# Keep the original logger name so log routing and caplog filters stay unchanged after the split.
logger = logging.getLogger("modules.chat.routes")

STREAM_POLL_INTERVAL = 0.1
# A stream lives only while its run is pending/streaming (terminal runs drain and return), so this cap
# bounds the polling delay added to first token and after mid-run pauses: <= 0.5 s (mean ~0.25 s).
STREAM_POLL_MAX_INTERVAL = 0.5
STREAM_BATCH_SIZE = 64
STREAM_BATCH_MAX_BYTES = 256 * 1024  # bounds the user-space buffer one stalled stream can pin
HEARTBEAT_INTERVAL = 15.0


_TERMINAL_RUN_STATUSES = ("completed", "cancelled", "failed")


@dataclass
class _PollState:
    """Mutable outcome of one SSE poll, shared between the stream loop and its locked poll.

    ``current_seq`` is the last event sequence delivered (carried across polls); the flags are
    per-poll results the loop reads after the poll finishes. ``stop`` ends the stream without a
    further status frame (the run, parent or privacy fence is gone or was already reported, or a
    terminal run's final batch was just delivered with nothing left queued).
    """

    current_seq: int
    batch_sent: bool = False
    batch_full: bool = False
    terminal_without_event: bool = False
    auth_expired: bool = False
    stop: bool = False


async def _authorize_stream_run(
    factory: async_sessionmaker[AsyncSession],
    request: Request,
    response_id: UUID,
) -> None:
    """Verify the requester owns a live run before any stream resources are taken.

    Uses one short session. The stream is the requester's own run only: actor and default
    workspace must both match, and an expired temporary parent conversation is refused.

    Raises:
        HTTPException: 401 when unauthenticated; 404 for an unknown or foreign run/conversation;
            410 for an expired temporary conversation.
    """
    requester_id = await _session_is_current(request)
    if requester_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    async with factory() as session:
        run = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Response run not found")
        if run.actor_user_id != requester_id:
            raise HTTPException(status_code=404, detail="Response run not found")
        await _run_scope(session, run)
        parent = await session.scalar(
            select(Conversation).where(Conversation.id == run.conversation_id)
        )
        if parent is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        _reject_expired_conversation(parent)


def _stream_start_seq(last_event_id: str | None, cursor: str | None) -> int:
    """First event sequence to replay: one past a valid resume cursor, else 1 (malformed -> full replay)."""
    selected_cursor = last_event_id or cursor
    if selected_cursor:
        try:
            _, parsed_seq = parse_event_id(selected_cursor)
            return parsed_seq + 1
        except ValueError:
            return 1
    return 1


async def _frames_for_batch(
    session: AsyncSession,
    run: ResponseRun,
    events: list[StreamEvent],
    state: _PollState,
) -> str:
    """Render ``events`` as one joined SSE chunk with citations re-filtered against current evidence.

    Filtering happens inside the caller's locked transaction so no object loaded before another
    transaction's redaction is replayed. Advances ``state.current_seq`` per rendered event and
    sets ``state.batch_full`` when the batch size or byte cap left events for the next poll.
    """
    citations = await _filter_citation_lists(
        session,
        [event.data.get("citations") if isinstance(event.data, dict) else None for event in events],
        await _run_scope(session, run),
    )
    frames: list[str] = []
    size = 0
    state.batch_full = len(events) == STREAM_BATCH_SIZE
    for event, current_citations in zip(events, citations, strict=True):
        if size >= STREAM_BATCH_MAX_BYTES:
            state.batch_full = True  # rest is re-read (and re-filtered) next poll
            break
        event_data = event.data
        if isinstance(event_data, dict) and isinstance(event_data.get("citations"), list):
            event_data = {**event_data, "citations": current_citations}
        frame = format_sse_event(
            event=event.event_type,
            data=event_data,
            event_id=event.event_id,
        )
        frames.append(frame)
        size += len(frame.encode())
        state.current_seq = event.seq
    return "".join(frames)


async def _publish_locked_events(
    session: AsyncSession,
    current_run: ResponseRun,
    response_id: UUID,
    token_hash: str | None,
    state: _PollState,
) -> AsyncGenerator[str, None]:
    """Inside the locked poll transaction, authenticate then yield the next batch of events.

    The auth row is read in the same transaction and snapshot as the event read (I5), so no frame
    follows a revocation. The joined chunk is yielded BEFORE the caller commits: the ASGI send
    then finds the transport drained and the bytes are written while every lock is still held.
    Sets ``state`` flags (``auth_expired``, ``batch_sent``, ``terminal_without_event``, and ``stop``
    when the delivered batch drained a terminal run).
    """
    account_id = await _auth_row_current(session, token_hash)
    if account_id is None or current_run.actor_user_id != account_id:
        state.auth_expired = True
        return
    events = list(
        (
            await session.scalars(
                select(StreamEvent)
                .where(
                    StreamEvent.response_id == response_id,
                    StreamEvent.seq > state.current_seq,
                )
                .order_by(StreamEvent.seq.asc())
                .limit(STREAM_BATCH_SIZE)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if events:
        chunk = await _frames_for_batch(session, current_run, events, state)
        # ONE send -> one non-blocking transport.write, under the locks.
        yield chunk
        state.batch_sent = True
        # A terminal run whose backlog fit in this batch has nothing left to send: end now instead of
        # idling one more fallback interval holding the permit and pub/sub. The loop reads ``stop``
        # only after the poll commits, so locks are released first.
        state.stop = not state.batch_full and current_run.status in _TERMINAL_RUN_STATUSES
    else:
        state.terminal_without_event = current_run.status in _TERMINAL_RUN_STATUSES


async def _locked_poll(
    factory: async_sessionmaker[AsyncSession],
    response_id: UUID,
    token_hash: str | None,
    state: _PollState,
) -> AsyncGenerator[str, None]:
    """One poll: lock-free probe, then (only if work exists) the locked publish transaction.

    Lock order is unchanged: Memory privacy, Conversation, ResponseRun, then evidence locks.
    Yields frames while the transaction is open and commits only after the consumer resumes.
    A failed privacy fence rolls back, redacts in a fresh transaction and emits a terminal
    ``cancelled`` status. Early exits set ``state.stop``; errors propagate to the stream loop.
    """
    async with factory() as probe:
        # Lock-free probe: an idle poll (nothing to publish or close) takes no lock.
        needs_lock = await _poll_needs_lock(probe, response_id, state.current_seq, token_hash)
    if not needs_lock:
        return
    async with factory() as session:
        run_hint = await session.scalar(select(ResponseRun).where(ResponseRun.id == response_id))
        if run_hint is None:
            state.stop = True
            return
        # Current-evidence output follows Memory privacy before Chat parent/run locks
        # for both active and terminal transcripts.
        await lock_export_privacy(session, run_hint.workspace_id, shared=True)
        parent = await session.scalar(
            select(Conversation)
            .where(
                Conversation.id == run_hint.conversation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent is None:
            state.stop = True
            return
        if parent.ephemeral and (
            parent.expires_at is None or parent.expires_at <= datetime.now(UTC)
        ):
            state.stop = True
            return
        current_run = await session.scalar(
            select(ResponseRun)
            .where(
                ResponseRun.id == response_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current_run is None:
            state.stop = True
            return
        if current_run.status in ACTIVE_RUN_STATUSES:
            stamp = (current_run.retrieval_context or {}).get("_chat_privacy_fence")
            try:
                await _require_privacy_fence(
                    session,
                    stamp,
                    scope=await _run_scope(session, current_run),
                )
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                # A failed read can poison the transaction; release locks and retry
                # redaction in a fresh transaction before emitting only terminal status.
                await session.rollback()
                await _mark_privacy_cancelled(response_id, factory, state.current_seq)
                yield format_sse_event("status", {"status": "cancelled"})
                state.stop = True
                return
        # aclosing guarantees the inner generator (and its yield-point state) is closed promptly
        # if the client disconnects while a chunk is pending, exactly like the former inline code.
        async with aclosing(
            _publish_locked_events(session, current_run, response_id, token_hash, state)
        ) as chunks:
            async for chunk in chunks:
                yield chunk
        await session.commit()


async def _sse_event_stream(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    response_id: UUID,
    token_hash: str | None,
    start_seq: int,
    started: asyncio.Event,
    semaphore: asyncio.Semaphore,
    redis: Any = None,
) -> AsyncGenerator[str, None]:
    """Publish batches of locked, revalidated events; wait for slow clients with no lock held.

    Each poll first yields an empty chunk OUTSIDE any transaction. Through the pure-ASGI stack,
    uvicorn's `send` awaits `flow.drain()` for earlier bytes before it writes (httptools_impl and
    h11_impl both drain first, then call the non-blocking `transport.write`), so a slow reader is
    waited for here, holding nothing. Then one transaction (``_locked_poll``) takes Memory privacy
    -> Conversation -> ResponseRun -> evidence locks (unchanged order), checks expiry, the privacy
    fence and the auth row, reads up to STREAM_BATCH_SIZE events, filters their citations once, and
    yields ONE joined chunk before commit: that `send` finds the transport just drained, so the
    bytes reach `transport.write` while every lock is held. A purge/redaction/revocation therefore
    commits entirely before this read (and is seen) or entirely after the write (and the next poll
    sees it). The stream permit is released when the generator ends for any reason.

    Idle waits: when ``redis`` is given the stream subscribes to ``chat:response:<id>`` BEFORE the first
    poll and waits on that wake-up (published by the worker after each commit), polling only every
    ``STREAM_SIGNAL_FALLBACK_INTERVAL`` as a safety net; without a subscription it keeps the original
    0.1-0.5 s backoff. The signal only decides WHEN to poll; the locked read path, ``seq`` cursor,
    ordering, dedup and resume semantics are untouched.
    """
    started.set()
    try:
        async with (
            signals.subscribed(redis, response_id) as signal,
            aclosing(
                _poll_loop(request, factory, response_id, token_hash, start_seq, signal),
            ) as frames,
        ):
            async for frame in frames:
                yield frame
    finally:
        semaphore.release()


async def _idle_wait(signal: signals.ResponseSignalSubscription, delay: float) -> None:
    """Pause between polls: wait on the Redis wake-up (slow fallback poll) or, unsubscribed, sleep ``delay``.

    Holds no transaction or lock. A wake-up or the fallback timeout both just trigger the next locked poll.
    """
    if signal.active:
        await signal.wait(signals.STREAM_SIGNAL_FALLBACK_INTERVAL)
    else:
        await asyncio.sleep(delay)


async def _poll_loop(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    response_id: UUID,
    token_hash: str | None,
    start_seq: int,
    signal: signals.ResponseSignalSubscription,
) -> AsyncGenerator[str, None]:
    """Run the poll/publish loop of one SSE connection; sleeps are signal-aware (see ``_sse_event_stream``)."""
    current_seq = start_seq - 1
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    delay = STREAM_POLL_INTERVAL

    while not await request.is_disconnected():
        yield ""  # drain point: no transaction or lock is open here
        state = _PollState(current_seq=current_seq)
        try:
            async with aclosing(_locked_poll(factory, response_id, token_hash, state)) as chunks:
                async for chunk in chunks:
                    yield chunk
        except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
            logger.warning("Error querying stream events: %s", type(exc).__name__)
            yield format_sse_event("status", {"status": "unavailable"})
            return
        current_seq = state.current_seq
        if state.stop:
            return
        if state.auth_expired:
            yield format_sse_event("status", {"status": "auth_expired"})
            return
        if state.batch_sent:
            last_heartbeat = loop.time()
            delay = STREAM_POLL_INTERVAL
            if state.batch_full:
                continue  # more is queued: poll again without sleeping
            # A partial batch drained the backlog; the worker flushes at most every STREAM_FLUSH_SECONDS (0.4 s).
            await _idle_wait(signal, delay)
            continue
        if state.terminal_without_event:
            return

        now = loop.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            yield ": heartbeat\n\n"
            last_heartbeat = now

        await _idle_wait(signal, delay)
        delay = min(delay * 2, STREAM_POLL_MAX_INTERVAL)
