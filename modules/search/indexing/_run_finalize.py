"""Run preparation, exhausted-generation finalisation and pending-item drop/fail transitions."""

from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.job_denial import admit_retry_stale, denial_code, terminalize
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.search.indexing._common import (
    EmbeddingAuthority,
    IndexProjection,
    _admit,
    _commit_index_change,
    _index_projection,
    capture_authority,
)
from modules.search.indexing._run_types import _RunContext
from modules.search.indexing._vector_index import (
    activate_generation,
    async_engine_for_session,
    build_vector_index_concurrently,
)
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings import public as ai_settings


async def _prepare_index_run(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    generation_id: UUID,
    workspace_id: UUID,
) -> tuple[_RunContext | None, int | None]:
    """Capture the original authority for one run and validate the generation against it.

    Returns ``(context, None)`` when the batch loop should run, otherwise ``(None, result)``
    with the value ``_index_generation`` must return (``None`` skipped, ``0`` after marking a
    non-active generation failed). Uses one session: rollback on every skip path; a
    ``model_unavailable`` failure is committed through ``_commit_index_change``. A typed
    admission denial terminalizes the queued/running generation (separate session) and skips.
    """
    async with factory() as session:
        owner = await workspaces.resolve_workspace_owner_context(
            session,
            workspace_id,
            multi_workspace_enabled=settings.multi_workspace_enabled,
        )
        if owner is None:
            await session.rollback()
            return None, None
        scope = InternalJobScope(
            workspace_id=workspace_id,
            actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
        )

        async def capture() -> EmbeddingAuthority | None:
            """Re-capture the embedding authority from a clean transaction; None if Search is off."""
            await session.rollback()
            captured = await capture_authority(session, settings, redis, scope=scope)
            if not await ai_settings.module_is_enabled(
                session,
                "search",
                scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
            ):
                await session.rollback()
                return None
            return captured

        try:
            captured = await admit_retry_stale(capture)
        except (DomainError, HTTPException) as exc:
            await session.rollback()
            code = denial_code(exc)
            if code is None:
                raise
            # Denial is terminal for a queued/running generation; the active one keeps serving.
            await terminalize(
                factory,
                IndexGeneration,
                generation_id,
                workspace_id,
                code,
                from_status=("queued", "running"),
            )
            return None, None
        if captured is None:
            return None, None
        original = captured
        generation = await session.scalar(
            select(IndexGeneration).where(
                IndexGeneration.id == generation_id,
                IndexGeneration.workspace_id == workspace_id,
            )
        )
        if generation is None or not original.permitted():
            await session.rollback()
            return None, None
        mapping, config = original.mapping, original.config
        if (
            mapping is None
            or mapping.model != generation.model_id
            or mapping.version != generation.model_version
            or config.model_identity != generation.gateway_identity
        ):  # column stores model_identity
            if generation.status != "active":
                before = await _index_projection(session, generation_id, scope=scope)
                generation.status, generation.error_code = "failed", "model_unavailable"
                await _commit_index_change(
                    session,
                    before,
                    generation_id=generation_id,
                    scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                    access_fence=original.fence,
                )
                return None, 0
            await session.rollback()
            return None, None
        await session.rollback()
    return _RunContext(factory, redis, settings, scope, original, generation_id, workspace_id), None


async def _finalize_exhausted_generation(
    session: AsyncSession,
    ctx: _RunContext,
    generation: IndexGeneration,
    before: IndexProjection | None,
    access_fence: AccessFence,
) -> None:
    """Finalize a running generation whose first (unfiltered) claim probe found nothing pending.

    Runs in the caller's locked session: prunes items whose chunk is no longer eligible, then marks
    the generation failed (``item_failed`` / ``no_indexable_chunks``) or activates it, and commits
    through ``_commit_index_change`` (which publishes the status change). A non-running generation
    is only committed (no state change). Activation first commits this transaction and builds the HNSW
    index with ``CREATE INDEX CONCURRENTLY`` on a separate autocommit connection (see
    ``_build_index_outside_transaction``); if the generation changed meanwhile the session is rolled back and
    nothing is published.
    """
    if generation.status == "running":
        await session.execute(
            text(
                "DELETE FROM search_index_items i WHERE i.generation_id = :generation_id "
                "AND i.status IN ('pending', 'failed') AND NOT EXISTS ("
                "SELECT 1 FROM document_chunks c "
                "JOIN document_versions v ON v.id = c.document_version_id "
                "JOIN documents d ON d.id = v.document_id "
                "JOIN sources s ON s.id = d.source_id "
                "WHERE c.id = i.chunk_id AND v.version_number = d.current_version "
                "AND d.workspace_id = :workspace_id AND s.workspace_id = :workspace_id "
                "AND d.extraction_status IN ('ready', 'succeeded') "
                "AND s.status = 'active' AND s.local_only = false AND s.rag_index_enabled = true)"
            ),
            {"generation_id": ctx.generation_id, "workspace_id": str(ctx.workspace_id)},
        )
        failed = await session.scalar(
            select(func.count())
            .select_from(SearchIndexItem)
            .where(
                SearchIndexItem.generation_id == ctx.generation_id,
                SearchIndexItem.status != "succeeded",
            )
        )
        if failed:
            generation.status = "failed"
            generation.error_code = "item_failed"
        elif generation.dimensions is None:
            generation.status = "failed"
            generation.error_code = "no_indexable_chunks"
        else:
            relocked = await _build_index_outside_transaction(session, ctx, generation)
            if relocked is None:
                return  # state changed during the build; the generation stays running and is re-finalized later
            generation, access_fence = relocked
            await activate_generation(session, generation, scope=ctx.scope)
    await _commit_index_change(
        session,
        before,
        generation_id=ctx.generation_id,
        scope=ctx.scope,
        multi_workspace_enabled=ctx.multi_workspace_enabled,
        access_fence=access_fence,
    )


async def _drop_pending_item(
    factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    settings: Settings,
    original: EmbeddingAuthority,
    item_id: UUID,
    generation_id: UUID,
    workspace_id: UUID,
) -> None:
    """Delete a pending item whose Source/chunk is no longer eligible, under the original fence."""
    async with factory() as session:
        try:
            fence = await _admit(
                session,
                scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
                lock=True,
                expected=original.fence,
            )
        except (DomainError, HTTPException):
            await session.rollback()
            return
        item = await session.scalar(
            select(SearchIndexItem)
            .join(
                IndexGeneration,
                IndexGeneration.id == SearchIndexItem.generation_id,
            )
            .where(
                SearchIndexItem.id == item_id,
                IndexGeneration.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if item is None:
            await session.rollback()
            return
        before = await _index_projection(session, generation_id, scope=scope)
        await session.delete(item)
        await _commit_index_change(
            session,
            before,
            generation_id=generation_id,
            scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled,
            access_fence=fence,
        )


async def _fail_item(
    factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    settings: Settings,
    original: EmbeddingAuthority,
    item_id: UUID,
    workspace_id: UUID,
) -> None:
    """Mark one item failed, but only while the original access fence still holds."""
    async with factory() as session:
        try:
            fence = await _admit(
                session,
                scope=scope,
                multi_workspace_enabled=settings.multi_workspace_enabled,
                lock=True,
                expected=original.fence,
            )
        except (DomainError, HTTPException):
            await session.rollback()
            return
        item = await session.scalar(
            select(SearchIndexItem)
            .join(
                IndexGeneration,
                IndexGeneration.id == SearchIndexItem.generation_id,
            )
            .where(
                SearchIndexItem.id == item_id,
                IndexGeneration.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if item is None:
            await session.rollback()
            return
        before = await _index_projection(session, item.generation_id, scope=scope)
        item.status = "failed"
        item.error_code = "embedding_failed"
        await _commit_index_change(
            session,
            before,
            generation_id=item.generation_id,
            scope=scope,
            multi_workspace_enabled=settings.multi_workspace_enabled,
            access_fence=fence,
        )


async def _build_index_outside_transaction(
    session: AsyncSession,
    ctx: _RunContext,
    generation: IndexGeneration,
) -> tuple[IndexGeneration, AccessFence] | None:
    """Commit, build the generation's HNSW index concurrently, then re-lock and re-validate for activation.

    Order: (1) commit the caller's transaction (releases the access-fence and generation locks and the
    write locks the concurrent build would otherwise wait on forever), (2) ``CREATE INDEX CONCURRENTLY`` on a
    separate autocommit connection, (3) new transaction: access fence (compared with the run's original
    fence) then generation row ``FOR UPDATE`` -- the same lock order as a claim. Returns the re-locked
    generation and fence, or ``None`` after a rollback when admission is denied, the generation is no longer
    running with the same dimensions, or unfinished items appeared meanwhile (a later pass claims them and
    finalizes again; the finished index is reused by ``IF NOT EXISTS``).
    """
    dimensions = generation.dimensions
    if dimensions is None:
        return None
    engine = async_engine_for_session(
        session
    )  # resolve before commit so a misconfiguration fails without side effects
    await session.commit()
    await build_vector_index_concurrently(engine, ctx.generation_id, dimensions)
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
    # populate_existing: the commit above kept the old instance (expire_on_commit=False), so refresh it
    # from the row we now hold the lock on instead of trusting pre-build attribute values.
    current = await session.scalar(
        select(IndexGeneration)
        .where(
            IndexGeneration.id == ctx.generation_id,
            IndexGeneration.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    unfinished = (
        await session.scalar(
            select(func.count())
            .select_from(SearchIndexItem)
            .where(
                SearchIndexItem.generation_id == ctx.generation_id,
                SearchIndexItem.status != "succeeded",
            )
        )
        if current is not None
        else 0
    )
    if (
        current is None
        or current.status != "running"
        or current.dimensions != dimensions
        or unfinished
    ):
        await session.rollback()
        return None
    return current, access_fence
