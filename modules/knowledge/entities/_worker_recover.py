"""Recovery scan that re-queues ready document versions and blocked entity extraction work."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.worker_context import resolve_owner_job_scope as _workspace_job_scope
from core.worker_context import worker_session_factory as _factory
from core.worker_cursors import STATE_KEY, read_cursor, write_cursor
from core.worker_queues import queue_for
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._worker_ready import (
    EXTRACTION_ROLE,
    _dependency_snapshot,
    _policy_allows_extraction,
)
from modules.knowledge.entities.extraction import (
    EXTRACTOR_VERSION,
    PROMPT_VERSION,
)
from modules.knowledge.entities.models import EntityExtractionWork
from modules.settings import public as settings_public
from modules.sources import public as sources

logger = logging.getLogger("modules.knowledge.entities.worker")  # same logger object as the facade


_WORKSPACE_CURSOR_KEY = "bbd:entity-extraction:workspace-cursor"
_WORKSPACE_PAGE = 100


async def _recovery_workspace_ids(
    ctx: dict[str, object], factory: async_sessionmaker[AsyncSession]
) -> list[UUID]:
    """One keyset page of workspaces with work rows or ready documents (cursor via core.worker_cursors)."""
    ctx.setdefault(STATE_KEY, {})
    keys = (_WORKSPACE_CURSOR_KEY,)
    after = await read_cursor(ctx, _WORKSPACE_CURSOR_KEY, keys)
    page: list[UUID] = []
    for lower in (after, None) if after is not None else (None,):
        async with factory() as session:
            query = (
                select(EntityExtractionWork.workspace_id)
                .distinct()
                .order_by(EntityExtractionWork.workspace_id)
                .limit(_WORKSPACE_PAGE)
            )
            if lower is not None:
                query = query.where(EntityExtractionWork.workspace_id > lower)
            found = set((await session.scalars(query)).all())
            found.update(
                await documents.list_ready_document_workspace_ids(
                    session, after=lower, limit=_WORKSPACE_PAGE
                )
            )
            await session.rollback()
        page = sorted(found)[:_WORKSPACE_PAGE]
        if page:
            break
    await write_cursor(ctx, _WORKSPACE_CURSOR_KEY, page[-1] if page else None, keys)
    return page


@dataclass(frozen=True)
class _ReadyPage:
    """One workspace's recovery page: its scope, cursor key, ready-version refs and the next cursor."""

    scope: InternalJobScope
    cursor_key: str
    refs: list[Any]
    next_cursor: str | None


async def _scan_ready_page(
    factory: async_sessionmaker[AsyncSession],
    redis: ArqRedis,
    workspace_id: UUID,
    multi_workspace_enabled: bool,
) -> _ReadyPage | None:
    """Resolve the workspace owner scope and read one bounded page of ready versions (committed).

    Returns ``None`` when the owner is gone or the module disabled so recovery skips the workspace. A stored
    cursor that is oversized (>512 chars) or malformed is ignored and the scan restarts from the beginning.
    """
    async with factory() as session:
        scope = await _workspace_job_scope(
            session,
            workspace_id,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if scope is None or not await settings_public.module_is_enabled(
            session,
            "knowledge.entities",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            return None
        cursor_key = f"bbd:entity-extraction:ready-cursor:{workspace_id}"
        raw_cursor = await redis.get(cursor_key)
        cursor = (
            raw_cursor.decode()
            if isinstance(raw_cursor, bytes)
            else raw_cursor
            if isinstance(raw_cursor, str)
            else None
        )
        if cursor is not None and len(cursor) > 512:
            cursor = None
        refs, next_cursor = await documents.list_ready_version_refs(
            session,
            limit=25,
            cursor=cursor,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    return _ReadyPage(scope=scope, cursor_key=cursor_key, refs=refs, next_cursor=next_cursor)


async def _recover_ready_ref(
    factory: async_sessionmaker[AsyncSession],
    scope: InternalJobScope,
    multi_workspace_enabled: bool,
    ref: Any,
) -> None:
    """Re-schedule extraction work for one ready version under the source/document fence (one commit).

    Stale refs (source gone/inactive or ready lineage drifted) commit and skip. Graph-disabled sources get no
    work. Local-only work that is not succeeded and not under a live lease is parked as blocked. The GitHub
    mapper runs in a savepoint so its failure cannot abort recovery of other sources.
    """
    async with factory() as session:
        access_fence = await workspaces.read_access_fence(
            session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        source = await sources.lock_source(
            session,
            ref.source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        current = await documents.get_ready_version_ref(
            session,
            ref.document_version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (
            source is None
            or source.status != "active"
            or current is None
            or current.document_id != ref.document_id
            or current.source_id != source.id
            or current.source_generation != source.generation
            or current.source_generation != ref.source_generation
        ):
            await session.commit()
            return
        work = (
            None
            if not source.graph_enabled
            else await entities.schedule_extraction_work(
                session,
                ref.document_version_id,
                source.generation,
                EXTRACTOR_VERSION,
                PROMPT_VERSION,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if (
            work is not None
            and current.local_only
            and work.status != "succeeded"
            and not (
                work.status == "running"
                and work.lease_expires_at is not None
                and work.lease_expires_at > datetime.now(UTC)
            )
        ):
            work.status = "blocked"
            work.error_code = "local_only_source"
            work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
            work.lease_owner = None
            work.lease_expires_at = None
            work.dependency_fingerprint = None
        from modules.connectors import public as connectors

        try:
            async with session.begin_nested():
                await connectors.map_github_version(
                    session,
                    current,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
        except Exception as exc:  # noqa: BLE001  # recovery proceeds for other sources
            logger.warning(
                "github mapping recovery failed for version %s (%s)",
                ref.document_version_id,
                type(exc).__name__,
            )
        await session.commit()


async def _requeue_blocked_item(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    scope: InternalJobScope,
    item: tuple[UUID, UUID, int, str | None, str | None],
    work_ids: list[UUID],
) -> None:
    """Requeue one policy/capability-blocked work item when its dependency fingerprint changed.

    Only ``ai_policy_denied``/``structured_unsupported`` rows with a saved fingerprint are considered. If the
    version/source is no longer eligible, or policy still denies or the requeue loses its compare-and-set, the
    recheck is deferred instead. A successful requeue appends the id to ``work_ids``. One commit.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    blocked_id, version_id, captured_generation, error_code, old_fingerprint = item
    if error_code not in {"ai_policy_denied", "structured_unsupported"} or old_fingerprint is None:
        return
    async with factory() as session:
        ready = await documents.get_ready_version_ref(
            session,
            version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        source = (
            await sources.lock_source(
                session,
                ready.source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if ready is not None
            else None
        )
        current = (
            await documents.get_ready_version_ref(
                session,
                version_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if ready is not None
            else None
        )
        if (
            ready is None
            or source is None
            or source.status != "active"
            or source.local_only
            or source.generation != captured_generation
            or current is None
            or current.document_id != ready.document_id
            or current.source_id != source.id
            or current.source_generation != captured_generation
        ):
            await entities.defer_blocked_extraction_recheck(
                session,
                blocked_id,
                old_fingerprint,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return
        config = settings_public.derive_for_role(
            await settings_public.get_ai_execution_config(
                session,
                settings,
                redis,
                scope=scope,
            ),
            EXTRACTION_ROLE,
        )
        fingerprint, capability_supported = await _dependency_snapshot(config, redis, scope=scope)
        mapping = config.defaults.resolve(EXTRACTION_ROLE)
        if (
            capability_supported
            and _policy_allows_extraction(
                config, mapping, config.endpoint_destination_id, source.local_only
            )
            and await entities.requeue_blocked_extraction_work(
                session,
                blocked_id,
                old_fingerprint,
                fingerprint,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        ):
            work_ids.append(blocked_id)
        else:
            await entities.defer_blocked_extraction_recheck(
                session,
                blocked_id,
                old_fingerprint,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        await session.commit()


async def _recover_workspace(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: ArqRedis,
    workspace_id: UUID,
) -> int:
    """Recover one workspace: re-schedule its ready page, requeue changed blocks, enqueue recoverable work.

    Order: page scan -> per-ref recovery -> blocked/exhausted/recoverable listing (one commit) -> dependency
    requeue -> cursor persist -> enqueue (deduplicated, model queue). Returns the number of enqueued jobs.
    """
    multi_workspace_enabled = settings.multi_workspace_enabled
    page = await _scan_ready_page(factory, redis, workspace_id, multi_workspace_enabled)
    if page is None:
        return 0
    scope = page.scope
    for ref in page.refs:
        await _recover_ready_ref(factory, scope, multi_workspace_enabled, ref)
    async with factory() as session:
        blocked_items = await entities.list_blocked_extraction_work(
            session,
            limit=25,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await entities.terminalize_exhausted_extraction_work(
            session,
            limit=25,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        work_ids = await entities.list_recoverable_extraction_work(
            session,
            limit=25,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        await session.commit()
    for item in blocked_items:
        await _requeue_blocked_item(factory, settings, cast(Redis, redis), scope, item, work_ids)
    if page.next_cursor:
        await redis.set(page.cursor_key, page.next_cursor)
    else:
        await redis.delete(page.cursor_key)
    enqueued = 0
    for work_id in dict.fromkeys(work_ids):
        await redis.enqueue_job(
            "process_entity_extraction_work",
            str(work_id),
            _job_id=f"entity-extraction:{work_id}",
            _queue_name=queue_for("process_entity_extraction_work"),
        )
        enqueued += 1
    return enqueued


async def recover_entity_extraction_work(ctx: dict[str, object]) -> int:
    """Recover bounded extraction work under each durable workspace binding.

    Visits one keyset page of workspaces per run and recovers each independently (see ``_recover_workspace``).
    Returns the total number of jobs enqueued.
    """
    factory = _factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    settings = cast(Settings, ctx["settings"])
    enqueued = 0
    for workspace_id in await _recovery_workspace_ids(ctx, factory):
        enqueued += await _recover_workspace(factory, settings, redis, workspace_id)
    return enqueued
