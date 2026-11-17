"""Single-chunk claim, embed, publish and process path."""

import time

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import select, text

from core.errors import DomainError
from core.model_gateway.client import ModelGatewayError, PrivacyPolicyDenied
from modules.knowledge.documents.public import (
    DocumentChunkReadProjection as DocumentChunk,
)
from modules.search.indexing._common import (
    INDEX_EVENT_ITEMS,
    INDEX_EVENT_SECONDS,
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
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings


async def _claim_next_chunk(ctx: _RunContext, state: _RunState) -> _ClaimedChunk | None:
    """Claim the next pending chunk (keyset after ``state.last_chunk``) in one locked transaction.

    Lock order: access fence -> generation row. Returns ``None`` when the loop must stop: denial
    (401/403/404/409), Search disabled, generation gone/not claimable, nothing above the keyset,
    or nothing pending (``state.idle`` set when the generation is not running; a running one is
    finalized). Other admission errors propagate. Advances ``state.last_chunk`` and creates the
    ``SearchIndexItem`` row; the queued->running claim is published, per-chunk progress is not.
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
                return None
            raise
        if not await ai_settings.module_is_enabled(
            session,
            "search",
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
        ):
            await session.rollback()
            return None
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
            return None
        claim_was_queued = generation.status == "queued"
        # Projection is needed to publish the queued->running claim or a first-probe finalize.
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
        row = (await session.execute(claim.order_by(DocumentChunk.id).limit(1))).first()
        if row is None and state.last_chunk is not None:
            # Chunk ids are random UUIDs and items may remain pending, so "nothing above the
            # keyset" does not mean "nothing pending": never finalize here; the next
            # invocation's first (unfiltered) probe decides finalize/idle.
            await session.rollback()
            return None
        if row is None and generation.status != "running":
            await session.rollback()
            state.idle = True  # nothing pending: let the page scan move on
            return None
        if row is None:
            await _finalize_exhausted_generation(session, ctx, generation, before, access_fence)
            return None
        chunk_id, content, source_id = row
        state.last_chunk = chunk_id
        item = await session.scalar(
            select(SearchIndexItem).where(
                SearchIndexItem.generation_id == ctx.generation_id,
                SearchIndexItem.chunk_id == chunk_id,
            )
        )
        if item is None:
            item = SearchIndexItem(generation_id=ctx.generation_id, chunk_id=chunk_id)
            session.add(item)
            await session.flush()
        claimed = _ClaimedChunk(chunk_id, content, source_id, item.id, generation.dimensions)
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


async def _embed_claimed_chunk(
    ctx: _RunContext,
    claimed: _ClaimedChunk,
) -> tuple[list[float], str | None, int | None] | None:
    """Embed one claimed chunk with no DB transaction open during the gateway call.

    Returns ``(values, returned_model, source_generation)``, or ``None`` after dropping a pending
    item whose Source/chunk is no longer eligible. A fresh short access/Source/chunk/config check
    runs before the actual request via ``before_send``; authority loss raises
    ``PrivacyPolicyDenied`` chained to ``_AuthorityChanged`` / ``_SourceStale``.
    """
    # Short preparation transaction: access -> Source -> chunk, then release every lock.
    async with ctx.factory() as session:
        _fence, live, source_generation = await _recheck_prepared(
            session,
            scope=ctx.scope,
            settings=ctx.settings,
            redis=ctx.redis,
            original=ctx.original,
            source_id=claimed.source_id,
            source_generation=None,
            chunk_id=claimed.chunk_id,
            content=claimed.content,
            workspace_id=ctx.workspace_id,
            lock=False,
        )
        await session.rollback()
    if not live or source_generation is None:
        await _drop_pending_item(
            ctx.factory,
            scope=ctx.scope,
            settings=ctx.settings,
            original=ctx.original,
            item_id=claimed.item_id,
            generation_id=ctx.generation_id,
            workspace_id=ctx.workspace_id,
        )
        return None

    async def recheck_send() -> None:
        """Fresh short access/Source/chunk/config check before each actual request."""
        async with ctx.factory() as check:
            try:
                _f, still_live, _g = await _recheck_prepared(
                    check,
                    scope=ctx.scope,
                    settings=ctx.settings,
                    redis=ctx.redis,
                    original=ctx.original,
                    source_id=claimed.source_id,
                    source_generation=source_generation,
                    chunk_id=claimed.chunk_id,
                    content=claimed.content,
                    workspace_id=ctx.workspace_id,
                    lock=False,
                )
            except _AuthorityChanged as exc:
                raise PrivacyPolicyDenied("Embedding send denied by current authority") from exc
            finally:
                await check.rollback()
            if not still_live:
                raise PrivacyPolicyDenied(
                    "Embedding source changed before send"
                ) from _SourceStale()

    mapping = ctx.original.mapping
    response = await gateway(ctx.original.config, ctx.redis, recheck_send, scope=ctx.scope).embed(
        "embedding",
        mapping,
        ctx.original.policy,
        [claimed.content],
    )
    values, returned_model = embedding_values(response, claimed.dimensions)
    return values, returned_model, source_generation


async def _publish_embedding(
    ctx: _RunContext,
    state: _RunState,
    claimed: _ClaimedChunk,
    source_generation: int | None,
    values: list[float],
    returned_model: str | None,
) -> bool:
    """Publish one embedding under the ordered locks; return False when the loop must stop.

    Lock order: access -> Source -> chunk -> generation -> item (re-verified against the original
    fence). Stale output (source gone, generation/item missing or no longer running) is discarded
    by deleting the item. A returned-model identity change fails the generation (always
    published) and stops the loop. Progress events are coalesced via ``state``; raises
    ``ValueError`` if the vector dimensions changed.
    """
    # Pre-serialize the pgvector literal before acquiring publication locks so JSON
    # encoding of up to 2,000 floats does not extend FOR UPDATE lock hold time.
    embedding_literal = _vector_literal(values)
    # Publication: same lock order (access -> Source -> chunk -> generation/item).
    async with ctx.factory() as session:
        fence, live, _g = await _recheck_prepared(
            session,
            scope=ctx.scope,
            settings=ctx.settings,
            redis=ctx.redis,
            original=ctx.original,
            source_id=claimed.source_id,
            source_generation=source_generation,
            chunk_id=claimed.chunk_id,
            content=claimed.content,
            workspace_id=ctx.workspace_id,
            lock=True,
        )
        generation = await session.scalar(
            select(IndexGeneration)
            .where(
                IndexGeneration.id == ctx.generation_id,
                IndexGeneration.workspace_id == ctx.workspace_id,
            )
            .with_for_update()
        )
        item = await session.scalar(
            select(SearchIndexItem)
            .join(
                IndexGeneration,
                IndexGeneration.id == SearchIndexItem.generation_id,
            )
            .where(
                SearchIndexItem.id == claimed.item_id,
                IndexGeneration.workspace_id == ctx.workspace_id,
            )
            .with_for_update()
        )
        if (
            not live
            or generation is None
            or item is None
            or generation.status not in {"running", "active"}
        ):
            if item is not None:
                before = await _index_projection(session, item.generation_id, scope=ctx.scope)
                await session.delete(item)
                await _commit_index_change(
                    session,
                    before,
                    generation_id=item.generation_id,
                    scope=ctx.scope,
                    multi_workspace_enabled=ctx.multi_workspace_enabled,
                    access_fence=fence,
                )
            else:
                await session.rollback()
            return True
        now = time.monotonic()
        publish = (
            state.unemitted + 1 >= INDEX_EVENT_ITEMS or now - state.last_emit >= INDEX_EVENT_SECONDS
        )
        before = (
            await _index_projection(session, ctx.generation_id, scope=ctx.scope)
            if publish
            else None
        )
        if generation.dimensions is None:
            generation.response_model_id = returned_model
            generation.dimensions = len(values)
        elif returned_model != generation.response_model_id:
            generation.status = "failed"
            generation.error_code = "model_identity_changed"
            item.status = "failed"
            item.error_code = "model_identity_changed"
            # Status change: always published (before=None differs from any projection).
            await _commit_index_change(
                session,
                before,
                generation_id=ctx.generation_id,
                scope=ctx.scope,
                multi_workspace_enabled=ctx.multi_workspace_enabled,
                access_fence=fence,
            )
            return False
        elif generation.dimensions != len(values):
            raise ValueError("Embedding dimensions changed during indexing")
        await session.execute(
            text(
                "UPDATE search_index_items SET embedding = CAST(:embedding AS vector) WHERE id = :item_id"
            ),
            {"embedding": embedding_literal, "item_id": claimed.item_id},
        )
        item.status = "succeeded"
        item.error_code = None
        await _commit_index_change(
            session,
            before,
            generation_id=ctx.generation_id,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=fence,
            publish=publish,
        )
        state.completed += 1
        if publish:
            state.unemitted, state.last_emit = 0, now
        else:
            state.unemitted += 1
    return True


async def _process_claimed_chunk(
    ctx: _RunContext, state: _RunState, claimed: _ClaimedChunk
) -> bool:
    """Embed and publish one claimed chunk, mapping failures to the loop's continue/stop decision.

    Returns True to continue with the next chunk, False to stop the batch. Authority changes and
    401/403/404/409 admission denials stop (the item stays pending for a fresh invocation); a
    stale Source drops the item; gateway/Redis/value errors fail the item; other errors propagate.
    """
    try:
        embedded = await _embed_claimed_chunk(ctx, claimed)
        if embedded is None:
            return True
        values, returned_model, source_generation = embedded
        return await _publish_embedding(
            ctx, state, claimed, source_generation, values, returned_model
        )
    except _AuthorityChanged:
        return False  # stale output discarded; the item stays pending for a fresh invocation
    except (DomainError, HTTPException) as exc:
        if exc.status_code in {401, 403, 404, 409}:
            return False
        raise
    except PrivacyPolicyDenied as exc:
        if isinstance(exc.__cause__, _AuthorityChanged):
            return False  # leave pending for a fresh invocation
        if isinstance(exc.__cause__, _SourceStale):
            await _drop_pending_item(
                ctx.factory,
                scope=ctx.scope,
                settings=ctx.settings,
                original=ctx.original,
                item_id=claimed.item_id,
                generation_id=ctx.generation_id,
                workspace_id=ctx.workspace_id,
            )
            return True
        await _fail_item(
            ctx.factory,
            scope=ctx.scope,
            settings=ctx.settings,
            original=ctx.original,
            item_id=claimed.item_id,
            workspace_id=ctx.workspace_id,
        )
    except (ModelGatewayError, RedisError, ValueError):
        await _fail_item(
            ctx.factory,
            scope=ctx.scope,
            settings=ctx.settings,
            original=ctx.original,
            item_id=claimed.item_id,
            workspace_id=ctx.workspace_id,
        )
    return True
