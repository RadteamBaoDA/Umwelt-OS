"""Verified Memory and candidate read projections, list filters and visible kind counts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, desc, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import DomainError
from core.workspaces.schemas import Scope
from modules.memory._common import _cache_key
from modules.memory._export import _memory_export_source_fence
from modules.memory._privacy_cleanup import _lock_live_provenance_evidence, lock_export_privacy
from modules.memory.models import Memory, MemoryCandidate
from modules.memory.schemas import MemoryCandidateRead, MemoryRead


async def invalidate_memory_cache(redis: Redis, *, scope: Scope) -> None:
    """Evict one workspace's active Memory data after cleanup commit; propagate failure for receipt retry."""
    await redis.delete(_cache_key(scope))


def _to_memory_read(item: Memory, provenance: dict[str, Any] | None = None) -> MemoryRead:
    """Project a Memory ORM instance into a safe MemoryRead schema.

    Args:
        item: Memory ORM model instance.
        provenance: Optional owner-verified provenance projection.

    Returns:
        MemoryRead schema instance.
    """
    return MemoryRead(
        id=item.id,
        content=item.content,
        memory_type=item.memory_type,  # populated by alias; `type=` left the required alias field missing
        provenance=provenance if provenance is not None else (item.provenance or {}),
        confidence=item.confidence,
        reason=item.reason,
        status=item.status,
        is_manual=item.is_manual,
        superseded_by_id=item.superseded_by_id,
        candidate_id=item.candidate_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
        invalidated_at=item.invalidated_at,
        forgotten_at=item.forgotten_at,
    )


async def _verified_memory_read(
    session: AsyncSession,
    item: Memory,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> MemoryRead | None:
    """Return a current-evidence projection or suppress a copied record whose origin is unverified."""
    try:
        await lock_export_privacy(session, scope.workspace_id, shared=True)
        if item.is_manual is not True:
            await _lock_live_provenance_evidence(
                session,
                item.provenance,
                require_copy_evidence=True,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        elif isinstance(item.provenance, dict):
            try:
                await _lock_live_provenance_evidence(
                    session,
                    item.provenance,
                    require_copy_evidence=False,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            except (HTTPException, DomainError) as exc:
                if exc.status_code != 409:
                    raise
        _, provenance = await _memory_export_source_fence(
            session,
            item,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except (HTTPException, DomainError) as exc:
        if exc.status_code == 409:
            return None
        raise
    return _to_memory_read(
        item,
        provenance.model_dump(mode="json", exclude_none=True) if provenance else {},
    )


async def _verified_candidate_read(
    session: AsyncSession,
    item: MemoryCandidate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> MemoryCandidateRead | None:
    """Return a candidate only while its copied evidence still has an owner-valid projection."""
    try:
        await lock_export_privacy(session, scope.workspace_id, shared=True)
        await _lock_live_provenance_evidence(
            session,
            item.provenance,
            require_copy_evidence=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        _, provenance = await _memory_export_source_fence(
            session,
            item,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except (HTTPException, DomainError) as exc:
        if exc.status_code == 409:
            return None
        raise
    return _to_candidate_read(
        item,
        provenance.model_dump(mode="json", exclude_none=True) if provenance else {},
    )


def _to_candidate_read(
    item: MemoryCandidate,
    provenance: dict[str, Any] | None = None,
) -> MemoryCandidateRead:
    """Project a MemoryCandidate ORM instance into a safe MemoryCandidateRead schema.

    Args:
        item: MemoryCandidate ORM model instance.
        provenance: Optional owner-verified provenance projection.

    Returns:
        MemoryCandidateRead schema instance.
    """
    return MemoryCandidateRead(
        id=item.id,
        content=item.content,
        memory_type=item.memory_type,  # populated by alias; `type=` left the required alias field missing
        provenance=provenance if provenance is not None else (item.provenance or {}),
        confidence=item.confidence,
        novelty_score=item.novelty_score,
        usefulness_score=item.usefulness_score,
        reason=item.reason,
        status=item.status,
        rejection_reason=item.rejection_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
        evaluated_at=item.evaluated_at,
    )


def _list_filters(status: str, query: str | None, *, scope: Scope) -> list[ColumnElement[bool]]:
    """Workspace-scoped visibility filters shared by the Memory list page and its per-kind counts (type excluded)."""
    filters: list[ColumnElement[bool]] = [
        Memory.workspace_id == scope.workspace_id,
        Memory.status == status,
    ]
    if query:
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(Memory.content.ilike(f"%{escaped}%", escape="\\"))
    return filters


COUNT_VERIFIED_CAP = 200
COUNT_SCAN_CAP = 400  # 2x verified cap bounds the hold time on the global privacy key
_COUNT_BATCH = 100


async def _visible_kind_counts(
    session: AsyncSession,
    filters: list[ColumnElement[bool]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[str, int] | None:
    """Tally kinds over rows that pass the same per-row privacy check as the list.

    Never counts rows the list would hide. Returns None when more than COUNT_VERIFIED_CAP
    visible rows (or COUNT_SCAN_CAP scanned rows) exist, so no number can leak hidden rows
    or exceed what the list shows.
    """
    counts: dict[str, int] = {}
    visible = scanned = 0
    last: tuple[datetime, UUID] | None = None
    while True:
        stmt = select(Memory).where(*filters)
        if last is not None:
            stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < last)
        batch = list(
            (
                await session.scalars(
                    stmt.order_by(desc(Memory.created_at), desc(Memory.id)).limit(_COUNT_BATCH)
                )
            ).all()
        )
        for row in batch:
            scanned += 1
            if (
                await _verified_memory_read(
                    session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                )
                is None
            ):
                continue
            visible += 1
            if visible > COUNT_VERIFIED_CAP:
                return None
            counts[row.memory_type] = counts.get(row.memory_type, 0) + 1
        if len(batch) < _COUNT_BATCH:
            return counts
        if scanned >= COUNT_SCAN_CAP:
            return None
        last = (batch[-1].created_at, batch[-1].id)
