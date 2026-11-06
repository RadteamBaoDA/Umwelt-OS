"""Observation cleanup, purge and retention contracts behind ``modules.knowledge.observations.public``."""

# isort: split
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    ColumnElement,
    SQLColumnExpression,
    Table,
    and_,
    delete,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import AccessFence, Scope
from modules.knowledge.observations._common import (
    _admit,
)
from modules.knowledge.observations.models import Observation
from modules.sources.public import SourceFence


@dataclass(frozen=True)
class ObservationCleanupClosure:
    """Workspace-qualified Observation IDs deleted with one Source or Document."""

    source_id: UUID
    document_id: UUID | None
    observation_ids: tuple[UUID, ...]
    overflow: bool


_CLEANUP_LIMIT = 10_000


def _require_cleanup_fences(
    actual: AccessFence,
    *,
    closure: ObservationCleanupClosure,
    scope: Scope,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Require the live access fence and Source to still match those held by a cleanup transaction.

    Raises HTTP 409 "Cleanup authority changed" (kept as ``HTTPException``: purge callers in sources/documents/
    ingestion branch on ``HTTPException`` status codes) when the fence differs or the Source id/workspace does not
    match the closure. Pure comparison; no I/O.
    """
    if (
        actual != access_fence
        or source_fence.id != closure.source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise HTTPException(status_code=409, detail="Cleanup authority changed")


async def observation_cleanup_ids(
    session: AsyncSession,
    *,
    source_id: UUID,
    document_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ObservationCleanupClosure:
    """Discover (nonlocking, ID-only) the Observations removed with a Source or Document."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    where = [Observation.workspace_id == scope.workspace_id, Observation.source_id == source_id]
    if document_id is not None:
        where.append(Observation.document_id == document_id)
    ids = list(
        (
            await session.scalars(
                select(Observation.id)
                .where(*where)
                .order_by(Observation.id)
                .limit(_CLEANUP_LIMIT + 1)
            )
        ).all()
    )
    return ObservationCleanupClosure(
        source_id=source_id,
        document_id=document_id,
        observation_ids=tuple(ids[:_CLEANUP_LIMIT]),
        overflow=len(ids) > _CLEANUP_LIMIT,
    )


async def prepare_document_cleanup_in_uow(
    session: AsyncSession,
    closure: ObservationCleanupClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Lock the discovered Observation rows in UUID order; no mutation."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if closure.overflow:
        raise ValueError("Observation cleanup exceeds its atomic limit")
    if closure.observation_ids:
        await session.scalars(
            select(Observation.id)
            .where(
                Observation.workspace_id == scope.workspace_id,
                Observation.id.in_(closure.observation_ids),
            )
            .order_by(Observation.id)
            .with_for_update()
        )


async def _held_observation_ids(
    session: AsyncSession,
    closure: ObservationCleanupClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> tuple[UUID, ...]:
    """Re-check the cleanup fences, re-discover the closure, and return its observation ids unchanged.

    Raises HTTP 409 when the access/source fences changed and ``RuntimeError("cleanup closure changed")`` when the
    re-discovered closure overflows or differs, so the caller only touches rows locked in the original closure.
    """
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    current = await observation_cleanup_ids(
        session,
        source_id=closure.source_id,
        document_id=closure.document_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current.overflow or current != closure:
        raise RuntimeError("cleanup closure changed")
    return closure.observation_ids


async def purge_source_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    closure: ObservationCleanupClosure,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Remove every prepared observation revision in the Source's deletion transaction; never locks."""
    if closure.document_id is not None or closure.source_id != source_id:
        raise ValueError("A Source-wide closure for this Source is required")
    ids = await _held_observation_ids(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if ids:
        await session.execute(
            update(Observation)
            .where(
                Observation.workspace_id == scope.workspace_id,
                Observation.id.in_(ids),
            )
            .values(is_current=False)
        )
        await session.execute(
            cast(Table, Observation.__table__)
            .delete()
            .where(
                Observation.workspace_id == scope.workspace_id,
                Observation.id.in_(ids),
            )
        )


async def purge_document_in_uow(
    session: AsyncSession,
    document_id: UUID,
    *,
    source_id: UUID,
    closure: ObservationCleanupClosure,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Remove prepared evidence-supported measurement revisions before their document is deleted."""
    if closure.document_id != document_id or closure.source_id != source_id:
        raise ValueError("A closure for this Document is required")
    ids = await _held_observation_ids(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if ids:
        await session.execute(
            cast(Table, Observation.__table__)
            .delete()
            .where(
                Observation.workspace_id == scope.workspace_id,
                Observation.id.in_(ids),
            )
        )


def current_observation_references(
    column: SQLColumnExpression[Any],
    *,
    field: str,
) -> ColumnElement[bool]:
    """Return an EXISTS predicate: a *current* observation points at ``column`` via ``field``.

    ``field`` is ``"document_version_id"`` or ``"ingestion_observation_id"``; ``column`` is the
    caller's own id column (correlated). Lets other modules protect rows still backing a current
    observation without importing the private Observation model.
    """
    if field not in ("document_version_id", "ingestion_observation_id"):
        raise ValueError(f"Unsupported observation reference field: {field}")
    return (
        select(1)
        .where(getattr(Observation, field) == column, Observation.is_current.is_(True))
        .exists()
    )


async def purge_expired_noncurrent_observations(
    session: AsyncSession,
    cutoffs: Sequence[tuple[UUID, datetime]],
    *,
    limit: int,
) -> int:
    """Delete up to ``limit`` non-current observations older than their source's cutoff.

    Current observations are never deleted. Oldest first, ``FOR UPDATE SKIP LOCKED`` so cleanup
    never blocks ingestion. Returns rows deleted; the caller commits.
    """
    if not cutoffs:
        return 0
    expired = or_(
        *(
            and_(Observation.source_id == sid, Observation.observed_at < cutoff)
            for sid, cutoff in cutoffs
        )
    )
    doomed = (
        select(Observation.id)
        .where(Observation.is_current.is_(False), expired)
        .order_by(Observation.observed_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(delete(Observation).where(Observation.id.in_(doomed)))
    return int(getattr(result, "rowcount", 0) or 0)
