"""Batched claim, embed, publish and process path."""

import time
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError
from core.model_gateway.client import ModelGatewayError, PrivacyPolicyDenied
from core.workspaces.schemas import AccessFence
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.search.indexing._common import (
    INDEX_EVENT_ITEMS,
    INDEX_EVENT_SECONDS,
    MAX_EMBED_BATCH,
    _admit,
    _commit_index_change,
    _index_projection,
    _vector_literal,
    eligible_chunks,
    embedding_values,
    gateway,
)
from modules.search.indexing._run_finalize import (
    _drop_pending_item,
    _fail_item,
    _finalize_exhausted_generation,
)
from modules.search.indexing._run_types import (
    _AuthorityChanged,
    _ClaimedChunk,
    _recheck_prepared,
    _RunContext,
    _RunState,
    _SourceStale,
)
from modules.search.indexing._single import _process_claimed_chunk
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings


def embedding_batch_values(
    response: object,
    count: int,
    expected_dimensions: int | None = None,
) -> tuple[list[list[float]], str | None]:
    """Validate a ``count``-input embedding response and return the vectors in input order plus the model.

    Every row passes the same finite/nonzero/dimension checks as ``embedding_values``. Rows are ordered by
    their ``index`` when all rows carry an integer one, else they must already be in input order. Raises
    ``ValueError`` for a wrong row count, duplicate/missing indexes or any invalid row, so a malformed
    batch never publishes a partial result.
    """
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list) or len(data) != count or count < 1:
        raise ValueError("Invalid embedding response")
    indexes = [row.get("index") if isinstance(row, dict) else None for row in data]
    if all(isinstance(i, int) and not isinstance(i, bool) for i in indexes):
        if sorted(cast(list[int], indexes)) != list(range(count)):
            raise ValueError("Invalid embedding response")
        data = [
            row
            for _, row in sorted(
                zip(cast(list[int], indexes), data, strict=True), key=lambda p: p[0]
            )
        ]
    model = cast(dict[str, object], response).get("model")
    vectors: list[list[float]] = []
    for row in data:
        values, _ = embedding_values({"data": [row], "model": model}, expected_dimensions)
        vectors.append(values)
        expected_dimensions = len(values)  # all rows of one response share one dimensionality
    return vectors, cast(str | None, model)


@dataclass(frozen=True)
class _EmbeddedBatch:
    """Vectors for the live chunks of one batch (parallel lists) and the response's model identity."""

    chunks: list[_ClaimedChunk]
    generations: list[int | None]
    vectors: list[list[float]]
    returned_model: str | None


def embed_batch_size(settings: object) -> int:
    """Configured chunks per embedding request, clamped to 1..128 (1 selects the single-chunk path)."""
    value = getattr(settings, "search_embed_batch_size", 1)
    return max(1, min(value, MAX_EMBED_BATCH)) if isinstance(value, int) else 1


async def _claim_chunk_batch(ctx: _RunContext, state: _RunState, limit: int) -> list[_ClaimedChunk]:
    """Claim up to ``limit`` pending chunks (one keyset query, ascending id) in one locked transaction.

    Lock order and stop conditions match ``_claim_next_chunk``: access fence -> generation row; an empty
    list means the loop must stop (denial, Search disabled, generation gone, nothing above the keyset, or
    nothing pending, in which case ``state.idle``/finalize apply exactly as for a single claim). Creates
    the missing ``SearchIndexItem`` rows in one flush, advances ``state.last_chunk`` to the batch's last
    id and publishes only the queued->running transition.
    """
    async with ctx.factory() as session:
        try:
            access_fence = await _admit(
                session,
                scope=ctx.scope,
                multi_workspace_enabled=ctx.multi_workspace_enabled,
                lock=True,
                expected=ctx.original.fence,
            )
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            if exc.status_code in {401, 403, 404, 409}:
                return []
            raise
        if not await ai_settings.module_is_enabled(
            session,
            "search",
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
        ):
            await session.rollback()
            return []
        generation = await session.scalar(
            select(IndexGeneration)
            .where(
                IndexGeneration.id == ctx.generation_id,
                IndexGeneration.workspace_id == ctx.workspace_id,
            )
            .with_for_update()
        )
        if generation is None or generation.status not in {"queued", "running", "active"}:
            await session.rollback()
            return []
        claim_was_queued = generation.status == "queued"
        before = (
            await _index_projection(session, ctx.generation_id, scope=ctx.scope)
            if claim_was_queued or state.last_chunk is None
            else None
        )
        if claim_was_queued:
            generation.status = "running"
        claim = (
            eligible_chunks(workspace_id=ctx.workspace_id)
            .outerjoin(
                SearchIndexItem,
                (SearchIndexItem.chunk_id == DocumentChunk.id)
                & (SearchIndexItem.generation_id == ctx.generation_id),
            )
            .where((SearchIndexItem.id.is_(None)) | (SearchIndexItem.status == "pending"))
        )
        if state.last_chunk is not None:
            claim = claim.where(DocumentChunk.id > state.last_chunk)
        rows = list((await session.execute(claim.order_by(DocumentChunk.id).limit(limit))).all())
        if not rows:
            if state.last_chunk is not None:
                await (
                    session.rollback()
                )  # keyset exhausted, not necessarily nothing pending (see single claim)
            elif generation.status != "running":
                await session.rollback()
                state.idle = True
            else:
                await _finalize_exhausted_generation(session, ctx, generation, before, access_fence)
            return []
        state.last_chunk = rows[-1][0]
        known = {
            item.chunk_id: item
            for item in await session.scalars(
                select(SearchIndexItem).where(
                    SearchIndexItem.generation_id == ctx.generation_id,
                    SearchIndexItem.chunk_id.in_([row[0] for row in rows]),
                )
            )
        }
        for chunk_id, _content, _source in rows:
            if chunk_id not in known:
                known[chunk_id] = SearchIndexItem(
                    generation_id=ctx.generation_id, chunk_id=chunk_id
                )
                session.add(known[chunk_id])
        await session.flush()
        claimed = [
            _ClaimedChunk(cid, content, sid, known[cid].id, generation.dimensions)
            for cid, content, sid in rows
        ]
        await _commit_index_change(
            session,
            before,
            generation_id=ctx.generation_id,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=access_fence,
            publish=claim_was_queued,
        )
    return claimed


async def _recheck_batch(
    ctx: _RunContext,
    chunks: list[_ClaimedChunk],
    generations: list[int | None],
) -> tuple[list[bool], list[int | None]]:
    """Revalidate every chunk of a batch in ONE short non-locking session (access -> Source -> chunk order).

    Returns per-chunk liveness and the Source generation observed; raises ``_AuthorityChanged`` when the
    access fence, module or configuration moved. Rolls back before returning, so no lock outlives the call.
    """
    live: list[bool] = []
    seen: list[int | None] = []
    async with ctx.factory() as session:
        try:
            for chunk, generation in zip(chunks, generations, strict=True):
                _fence, ok, source_generation = await _recheck_prepared(
                    session,
                    scope=ctx.scope,
                    settings=ctx.settings,
                    redis=ctx.redis,
                    original=ctx.original,
                    source_id=chunk.source_id,
                    source_generation=generation,
                    chunk_id=chunk.chunk_id,
                    content=chunk.content,
                    workspace_id=ctx.workspace_id,
                    lock=False,
                )
                live.append(ok and source_generation is not None)
                seen.append(source_generation)
        finally:
            await session.rollback()
    return live, seen


async def _embed_chunk_batch(
    ctx: _RunContext,
    claimed: list[_ClaimedChunk],
    survivors: list[_ClaimedChunk] | None = None,
) -> _EmbeddedBatch | None:
    """Embed the live chunks of a claim with one gateway request and no DB transaction open during it.

    Chunks whose Source/chunk is no longer eligible have their pending item dropped. Returns ``None`` when
    nothing is live. A fresh batch-wide recheck runs as ``before_send``; any authority loss raises
    ``PrivacyPolicyDenied`` chained to ``_AuthorityChanged`` and a stale chunk to ``_SourceStale`` (the
    caller falls back to per-chunk processing). ``ValueError`` signals a malformed response. When given,
    ``survivors`` is replaced in place by the chunks that were still live after the first recheck, so a
    fallback never re-sends chunks whose pending items were just dropped.
    """
    live, generations = await _recheck_batch(ctx, claimed, [None] * len(claimed))
    chunks: list[_ClaimedChunk] = []
    kept: list[int | None] = []
    for chunk, ok, generation in zip(claimed, live, generations, strict=True):
        if ok:
            chunks.append(chunk)
            kept.append(generation)
        else:
            await _drop_pending_item(
                ctx.factory,
                scope=ctx.scope,
                settings=ctx.settings,
                original=ctx.original,
                item_id=chunk.item_id,
                generation_id=ctx.generation_id,
                workspace_id=ctx.workspace_id,
            )
    if survivors is not None:
        survivors[:] = chunks
    if not chunks:
        return None

    async def recheck_send() -> None:
        """Fresh short access/Source/chunk/config check for the whole batch before the actual request."""
        try:
            still_live, _g = await _recheck_batch(ctx, chunks, kept)
        except _AuthorityChanged as exc:
            raise PrivacyPolicyDenied("Embedding send denied by current authority") from exc
        if not all(still_live):
            raise PrivacyPolicyDenied("Embedding source changed before send") from _SourceStale()

    response = await gateway(ctx.original.config, ctx.redis, recheck_send, scope=ctx.scope).embed(
        "embedding",
        ctx.original.mapping,
        ctx.original.policy,
        [chunk.content for chunk in chunks],
    )
    vectors, returned_model = embedding_batch_values(response, len(chunks), chunks[0].dimensions)
    return _EmbeddedBatch(chunks, kept, vectors, returned_model)


async def _lock_batch_sources(
    session: AsyncSession,
    ctx: _RunContext,
    batch: _EmbeddedBatch,
) -> tuple[AccessFence, dict[UUID, bool]]:
    """Re-admit under lock and revalidate each chunk in canonical (Source id, chunk id) order.

    Sorting by Source id before locking keeps the access -> Source -> Document order identical for every
    concurrent publisher, so two multi-Source batches cannot deadlock. Returns the fence and
    ``{chunk id: still live}``; raises ``_AuthorityChanged`` from ``_recheck_prepared``.
    """
    order = sorted(
        range(len(batch.chunks)),
        key=lambda i: (str(batch.chunks[i].source_id), str(batch.chunks[i].chunk_id)),
    )
    fence: AccessFence | None = None
    live: dict[UUID, bool] = {}
    for i in order:
        chunk = batch.chunks[i]
        fence, ok, _g = await _recheck_prepared(
            session,
            scope=ctx.scope,
            settings=ctx.settings,
            redis=ctx.redis,
            original=ctx.original,
            source_id=chunk.source_id,
            source_generation=batch.generations[i],
            chunk_id=chunk.chunk_id,
            content=chunk.content,
            workspace_id=ctx.workspace_id,
            lock=True,
        )
        live[chunk.chunk_id] = ok
    if fence is None:
        raise _AuthorityChanged
    return fence, live


async def _publish_embedding_batch(
    ctx: _RunContext, state: _RunState, batch: _EmbeddedBatch
) -> bool:
    """Publish a whole batch in ONE transaction; return False when the loop must stop.

    Lock order: access -> Sources (sorted) -> Documents -> generation -> items (sorted by id), all
    re-verified against the original fence. Stale output (chunk no longer live, generation/item gone or
    not running/active) deletes its item. A returned-model identity change fails the generation and every
    item of the batch (published) and stops; a dimension change raises ``ValueError``. Live vectors are
    written with a single executemany UPDATE, and progress events are coalesced via ``state``.
    """
    literals = [
        _vector_literal(values) for values in batch.vectors
    ]  # serialize before taking locks
    async with ctx.factory() as session:
        fence, live = await _lock_batch_sources(session, ctx, batch)
        generation = await session.scalar(
            select(IndexGeneration)
            .where(
                IndexGeneration.id == ctx.generation_id,
                IndexGeneration.workspace_id == ctx.workspace_id,
            )
            .with_for_update()
        )
        items = {
            item.id: item
            for item in await session.scalars(
                select(SearchIndexItem)
                .join(
                    IndexGeneration,
                    IndexGeneration.id == SearchIndexItem.generation_id,
                )
                .where(
                    SearchIndexItem.id.in_([chunk.item_id for chunk in batch.chunks]),
                    IndexGeneration.workspace_id == ctx.workspace_id,
                )
                .order_by(SearchIndexItem.id)
                .with_for_update()
            )
        }
        running = generation is not None and generation.status in {"running", "active"}
        writes: list[dict[str, object]] = []
        stale: list[SearchIndexItem] = []
        for chunk, literal in zip(batch.chunks, literals, strict=True):
            item = items.get(chunk.item_id)
            if item is None:
                continue
            if live[chunk.chunk_id] and running:
                writes.append({"embedding": literal, "item_id": chunk.item_id})
            else:
                stale.append(item)
        now = time.monotonic()
        publish = (
            state.unemitted + len(writes) >= INDEX_EVENT_ITEMS
            or now - state.last_emit >= INDEX_EVENT_SECONDS
        )
        before = (
            await _index_projection(session, ctx.generation_id, scope=ctx.scope)
            if publish or stale
            else None
        )
        for item in stale:
            await session.delete(item)
        if writes and generation is not None:
            dimensions = len(batch.vectors[0])
            if generation.dimensions is None:
                generation.response_model_id, generation.dimensions = (
                    batch.returned_model,
                    dimensions,
                )
            elif batch.returned_model != generation.response_model_id:
                generation.status, generation.error_code = "failed", "model_identity_changed"
                for write in writes:
                    failed_item = items[cast(UUID, write["item_id"])]
                    failed_item.status, failed_item.error_code = "failed", "model_identity_changed"
                await _commit_index_change(
                    session,
                    before,
                    generation_id=ctx.generation_id,
                    scope=ctx.scope,
                    multi_workspace_enabled=ctx.multi_workspace_enabled,
                    access_fence=fence,
                )
                return False
            elif generation.dimensions != dimensions:
                raise ValueError("Embedding dimensions changed during indexing")
            await session.execute(
                text(
                    "UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"
                ),
                writes,
            )
            for write in writes:
                done = items[cast(UUID, write["item_id"])]
                done.status, done.error_code = "succeeded", None
        await _commit_index_change(
            session,
            before,
            generation_id=ctx.generation_id,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=fence,
            publish=publish or bool(stale),
        )
        state.completed += len(writes)
        if publish:
            state.unemitted, state.last_emit = 0, now
        else:
            state.unemitted += len(writes)
    return True


async def _fail_batch(ctx: _RunContext, claimed: list[_ClaimedChunk]) -> None:
    """Mark every item of a batch failed (each under the original fence, own short transaction)."""
    for chunk in claimed:
        await _fail_item(
            ctx.factory,
            scope=ctx.scope,
            settings=ctx.settings,
            original=ctx.original,
            item_id=chunk.item_id,
            workspace_id=ctx.workspace_id,
        )


async def _process_claimed_batch(
    ctx: _RunContext, state: _RunState, claimed: list[_ClaimedChunk]
) -> bool:
    """Embed and publish one claimed batch; return True to continue, False to stop the run.

    Authority changes and 401/403/404/409 admission denials stop (items stay pending). A stale Source or a
    malformed response degrades to the per-chunk path for this batch only, so one bad chunk cannot fail
    its neighbours. Gateway/Redis errors fail every item of the batch (as the per-chunk path would, one
    request at a time); other errors propagate.
    """
    survivors = list(
        claimed
    )  # narrowed to the live chunks once the batch recheck has dropped the stale ones
    try:
        embedded = await _embed_chunk_batch(ctx, claimed, survivors)
        if embedded is None:
            return True
        return await _publish_embedding_batch(ctx, state, embedded)
    except _AuthorityChanged:
        return False
    except (DomainError, HTTPException) as exc:
        if exc.status_code in {401, 403, 404, 409}:
            return False
        raise
    except PrivacyPolicyDenied as exc:
        if isinstance(exc.__cause__, _AuthorityChanged):
            return False
        if not isinstance(exc.__cause__, _SourceStale):
            await _fail_batch(ctx, claimed)
            return True
    except ValueError:
        pass  # malformed response or dimension drift: isolate the offender chunk by chunk below
    except (ModelGatewayError, RedisError):
        await _fail_batch(ctx, claimed)
        return True
    for (
        chunk
    ) in survivors:  # fallback: identical to the single-chunk pipeline, one request per chunk
        if not await _process_claimed_chunk(ctx, state, chunk):
            return False
    return True
