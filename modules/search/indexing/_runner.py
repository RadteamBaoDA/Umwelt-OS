"""Worker entrypoint and per-generation run loop."""

import logging
import time
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.cron import wake
from core.heavy_work import bounded_heavy_work
from core.worker_context import worker_session_factory
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from modules.knowledge.documents import public as documents
from modules.search.indexing._batch import (
    _claim_chunk_batch,
    _process_claimed_batch,
    embed_batch_size,
)
from modules.search.indexing._common import (
    _CURSOR_KEYS,
    ACTIVE_INDEX_CURSOR_KEY,
    INDEX_RUN_BATCH_ITEMS,
    INDEX_RUN_ITEMS,
    INDEX_RUN_SECONDS,
    QUEUED_INDEX_CURSOR_KEY,
    SCAN_PAGE,
)
from modules.search.indexing._generations import _reconcile_automatic_generations
from modules.search.indexing._run_finalize import _prepare_index_run
from modules.search.indexing._run_types import _RunState
from modules.search.indexing._single import _claim_next_chunk, _process_claimed_chunk
from modules.search.indexing._vector_index import (
    async_engine_for_session,
    build_vector_index_concurrently,
)
from modules.search.models import IndexGeneration

logger = logging.getLogger("modules.search.indexing")


async def _candidate_page(
    factory: async_sessionmaker[AsyncSession],
    statuses: tuple[str, ...],
    after: UUID | None,
) -> list[tuple[UUID, UUID]]:
    """Return one bounded keyset page of (generation, workspace) identities, wrapping at the end."""
    async with factory() as session:
        rows: list[tuple[UUID, UUID]] = []
        for lower in (after, None) if after is not None else (None,):
            query = select(IndexGeneration.id, IndexGeneration.workspace_id).where(
                IndexGeneration.status.in_(statuses),
            )
            if lower is not None:
                query = query.where(IndexGeneration.workspace_id > lower)
            result = await session.execute(
                query.order_by(IndexGeneration.workspace_id, IndexGeneration.id).limit(SCAN_PAGE)
            )
            rows = [(row[0], row[1]) for row in result.all()]
            if rows:
                break
        await session.rollback()
    return rows


async def index_pending_chunks(ctx: dict[str, object]) -> int:
    """Run one indexing pass (the cron entry point), then build any HNSW index it deferred.

    The pass runs under the heavy-work slot (``_index_pending_chunks_in_slot``); the slot is released
    when that call returns. Only then are the deferred ``CREATE INDEX CONCURRENTLY`` builds run, on
    their own connections, because the build waits for every older transaction and would otherwise
    wait on the slot's own transaction until its deadline. After a build the poller is woken so the
    next pass finds the valid index and activates the generation. A build error propagates: the
    generation stays ``running`` and the next scheduled tick retries. A busy-slot ``Retry`` from the
    pass passes through unchanged and no build is attempted.

    Returns the pass's completed count.
    """
    builds: list[tuple[UUID, int]] = []
    done = await _index_pending_chunks_in_slot(ctx, builds)  # slot released on return
    if builds:
        async with worker_session_factory(ctx)() as session:
            engine = async_engine_for_session(session)
        for generation_id, dimensions in builds:
            # ponytail: bounded by the 180 s cron job_timeout; split out a long-timeout job if needed
            await build_vector_index_concurrently(engine, generation_id, dimensions)
        # A wake that meets the held cron lease sets the rerun flag, so the activating pass runs next.
        await wake(ctx["redis"], "index_pending_chunks")
    return done


@bounded_heavy_work
async def _index_pending_chunks_in_slot(
    ctx: dict[str, object], builds: list[tuple[UUID, int]]
) -> int:
    """Reconcile, then index one eligible workspace generation chosen from a bounded fair page.

    Runs inside the heavy-work slot. Queued/running generations are scanned first by workspace keyset
    cursor, then active ones; a denied, disabled or unavailable subject is skipped without touching its
    rows, and the cursor advances past the scanned page so no single subject can monopolize indexing.
    A generation whose finalize deferred its HNSW build is appended to ``builds`` (generation id,
    dimensions) for the caller to build after the slot is released.
    """
    factory = worker_session_factory(ctx)
    redis = cast(Redis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    # ARQ copies ctx per job: without the worker-installed dict this is a per-job throwaway.
    if STATE_KEY not in ctx:
        logger.warning("w2_cursor_state missing; search cursors are per-job")
    state = cast(dict[str, str], ctx.setdefault(STATE_KEY, {}))
    async with factory() as session:
        await documents.backfill_current_chunks(
            session, multi_workspace_enabled=settings.multi_workspace_enabled
        )
    await _reconcile_automatic_generations(factory, settings, redis, ctx)

    classes = [
        (QUEUED_INDEX_CURSOR_KEY, ("queued", "running")),
        (ACTIVE_INDEX_CURSOR_KEY, ("active",)),
    ]
    turn = 1 if state.get("search_class_turn") == "1" else 0
    state["search_class_turn"] = str(1 - turn)  # alternate which class goes first each invocation
    for key, statuses in classes[turn:] + classes[:turn]:
        after = await read_cursor(ctx, key, _CURSOR_KEYS)
        last: UUID | None = None
        done: int | None = None
        for generation_id, workspace_id in await _candidate_page(factory, statuses, after):
            last = workspace_id
            done = await _index_generation(
                factory, redis, settings, generation_id, workspace_id, builds=builds
            )
            if done is not None:
                break
        if last is not None:
            await write_cursor(ctx, key, last, _CURSOR_KEYS)
        if done is not None:
            return done
    return 0


def _record_pending_build(
    builds: list[tuple[UUID, int]] | None, generation_id: UUID, state: _RunState
) -> None:
    """Append the generation's deferred HNSW build to ``builds`` when finalize left one pending.

    No-op when the caller passed no ``builds`` list (direct callers that do not build) or when this run
    did not defer a build.
    """
    if builds is not None and state.pending_build is not None:
        builds.append((generation_id, state.pending_build))


async def _index_generation(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    generation_id: UUID,
    workspace_id: UUID,
    *,
    builds: list[tuple[UUID, int]] | None = None,
) -> int | None:
    """Index a bounded batch of chunks (items and time) of one generation; None when skipped.

    A finalize that defers the HNSW build is reported through ``builds`` (see ``_record_pending_build``).

    Chunks are claimed in ascending id order via a keyset (``id > last_chunk``) so an item that
    stays pending (e.g. dropped under admission failure) is not re-claimed within the run.
    Progress events are coalesced; claim of a queued generation, finalize and failures publish.

    One original AccessFence/config snapshot is captured per invocation. No DB transaction or
    lock is open during ``embed``: preparation and the gateway ``before_send`` hook use short
    ordered sessions (access, Source, chunk), and publication reacquires the same order and
    compares the originals, discarding stale output instead of rebasing.
    """
    ctx, early = await _prepare_index_run(factory, redis, settings, generation_id, workspace_id)
    if ctx is None:
        return early
    state = _RunState()
    deadline = time.monotonic() + INDEX_RUN_SECONDS
    batch_size = embed_batch_size(settings)
    if batch_size > 1:
        # Batched path: loop while work remains, bounded by the time budget (it gates *starting* a batch).
        while state.completed < INDEX_RUN_BATCH_ITEMS and time.monotonic() < deadline:
            claimed_batch = await _claim_chunk_batch(ctx, state, batch_size)
            if not claimed_batch or not await _process_claimed_batch(ctx, state, claimed_batch):
                break
        _record_pending_build(builds, generation_id, state)
        return None if state.idle and state.completed == 0 else state.completed
    for _ in range(INDEX_RUN_ITEMS):
        if time.monotonic() >= deadline:
            break
        claimed = await _claim_next_chunk(ctx, state)
        if claimed is None:
            break
        if not await _process_claimed_chunk(ctx, state, claimed):
            break
    _record_pending_build(builds, generation_id, state)
    return None if state.idle and state.completed == 0 else state.completed
