"""Support cleanup closure discovery, locking and removal for relationships.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

from dataclasses import replace
from uuid import UUID

from sqlalchemy import (
    delete,
    false,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.relationships._common import (
    MAX_CLEANUP_SUPPORTS,
    RelationshipSupportClosure,
    _admit,
    _evidence_scope,
    _require_cleanup_fences,
    _support_pairs,
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
)
from modules.sources.public import SourceFence


async def _discover_support(
    session: AsyncSession,
    *,
    refs: list[tuple[UUID, UUID]],
    source_id: UUID,
    document_id: UUID | None,
    membership_ids: tuple[UUID, ...],
    scope: Scope,
    include_history: bool,
) -> RelationshipSupportClosure:
    """Nonlocking ID-only discovery; ``include_history`` False leaves history IDs empty."""
    match = or_(
        RelationshipEvidence.document_id == document_id
        if document_id
        else RelationshipEvidence.source_id == source_id,
        tuple_(RelationshipEvidence.document_version_id, RelationshipEvidence.chunk_id).in_(refs)
        if refs
        else false(),
        RelationshipEvidence.source_membership_id.in_(membership_ids)
        if membership_ids
        else false(),
        RelationshipEvidence.target_membership_id.in_(membership_ids)
        if membership_ids
        else false(),
    )
    rows = list(
        (
            await session.execute(
                select(RelationshipEvidence.id, RelationshipEvidence.relationship_id)
                .where(_evidence_scope(scope), match)
                .order_by(RelationshipEvidence.id)
                .limit(MAX_CLEANUP_SUPPORTS + 1)
            )
        ).all()
    )
    overflow = len(rows) > MAX_CLEANUP_SUPPORTS
    rows = rows[:MAX_CLEANUP_SUPPORTS]
    relationship_ids = sorted({row[1] for row in rows})
    endpoints: set[UUID] = set()
    if relationship_ids:
        endpoint_rows = (
            await session.execute(
                select(Relationship.source_entity_id, Relationship.target_entity_id).where(
                    Relationship.workspace_id == scope.workspace_id,
                    Relationship.id.in_(relationship_ids),
                )
            )
        ).all()
        endpoints = {identifier for row in endpoint_rows for identifier in row}
    history_ids: list[int] = []
    if len(set(refs)) > MAX_CLEANUP_SUPPORTS:
        overflow = True
    elif include_history and refs:
        pairs = _support_pairs(refs)
        after = 0
        # ponytail: JSON support scan; add a GIN index if retained history makes purge slow.
        while len(history_ids) <= MAX_CLEANUP_SUPPORTS:
            page = list(
                (
                    await session.scalars(
                        select(RelationshipSnapshotHistory.id)
                        .where(
                            RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
                            RelationshipSnapshotHistory.id > after,
                            or_(
                                *[
                                    RelationshipSnapshotHistory.support.contains(
                                        [
                                            {
                                                "document_version_id": version,
                                                "chunk_id": chunk,
                                            }
                                        ]
                                    )
                                    for version, chunk in sorted(pairs)
                                ]
                            ),
                        )
                        .order_by(RelationshipSnapshotHistory.id)
                        .limit(100)
                    )
                ).all()
            )
            if not page:
                break
            history_ids.extend(page)
            after = page[-1]
        if len(history_ids) > MAX_CLEANUP_SUPPORTS:
            overflow = True
            history_ids = history_ids[:MAX_CLEANUP_SUPPORTS]
    return RelationshipSupportClosure(
        source_id=source_id,
        document_id=document_id,
        membership_ids=tuple(sorted(membership_ids)),
        relationship_ids=tuple(relationship_ids),
        evidence_ids=tuple(row[0] for row in rows),
        endpoint_entity_ids=tuple(sorted(endpoints)),
        history_ids=tuple(history_ids),
        overflow=overflow,
    )


async def purge_history_support(
    session: AsyncSession,
    closure: RelationshipSupportClosure,
    refs: list[tuple[UUID, UUID]],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Remove purged evidence from the prepared retained-history rows without committing.

    History rows were found by bounded discovery and locked by ``prepare_support_cleanup_in_uow``.
    Derived fields sharing any removed support lose their entire state; owner-authored
    fields survive without removed citations. Identifier-only support remains.
    """
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    current = await _discover_support(
        session,
        refs=refs,
        source_id=closure.source_id,
        document_id=closure.document_id,
        membership_ids=closure.membership_ids,
        scope=scope,
        include_history=True,
    )
    if current.overflow or current != closure:
        raise RuntimeError("cleanup closure changed")
    pairs = _support_pairs(refs)
    for offset in range(0, len(closure.history_ids), 100):
        rows = (
            await session.scalars(
                select(RelationshipSnapshotHistory)
                .where(
                    RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
                    RelationshipSnapshotHistory.id.in_(closure.history_ids[offset : offset + 100]),
                )
                .order_by(RelationshipSnapshotHistory.id)
            )
        ).all()
        for row in rows:
            row.support = [
                item
                for item in row.support
                if (
                    str(item.get("document_version_id")),
                    str(item.get("chunk_id")),
                )
                not in pairs
            ]
            if row.state.get("origin") != "owner":
                row.state = {}  # Past derived text cannot survive evidence purge.
    await session.flush()


async def support_cleanup_ids(
    session: AsyncSession,
    *,
    refs: list[tuple[UUID, UUID]],
    source_id: UUID,
    document_id: UUID | None = None,
    membership_ids: list[UUID] | tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipSupportClosure:
    """Discover (nonlocking, ID-only) relationship support, endpoints and retained history."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await _discover_support(
        session,
        refs=refs,
        source_id=source_id,
        document_id=document_id,
        membership_ids=tuple(membership_ids),
        scope=scope,
        include_history=True,
    )


async def prepare_support_cleanup_in_uow(
    session: AsyncSession,
    closure: RelationshipSupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Lock relationships, then evidence, then retained history rows in order; no mutation."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if closure.overflow:
        raise ValueError("Relationship support cleanup exceeds its atomic limit")
    if closure.relationship_ids:
        await session.scalars(
            select(Relationship.id)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id.in_(closure.relationship_ids),
            )
            .order_by(Relationship.id)
            .with_for_update()
        )
    if closure.evidence_ids:
        await session.scalars(
            select(RelationshipEvidence.id)
            .where(
                _evidence_scope(scope),
                RelationshipEvidence.id.in_(closure.evidence_ids),
            )
            .order_by(RelationshipEvidence.id)
            .with_for_update()
        )
    if closure.history_ids:
        await session.scalars(
            select(RelationshipSnapshotHistory.id)
            .where(
                RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
                RelationshipSnapshotHistory.id.in_(closure.history_ids),
            )
            .order_by(RelationshipSnapshotHistory.id)
            .with_for_update()
        )


async def lock_relationship_ids(
    session: AsyncSession,
    relationship_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Lock a bounded sorted relationship set in one workspace for cleanup or correction."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(relationship_ids), key=str)
    if len(ids) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship support cleanup exceeds its atomic limit")
    if ids:
        await session.scalars(
            select(Relationship.id)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id.in_(ids),
            )
            .order_by(Relationship.id)
            .with_for_update()
        )


async def lock_delete_closure(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[UUID], list[UUID]]:
    """Lock and return the exact incident edge/support IDs after entity locks."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Entity deletion closure exceeds its atomic entity limit")
    edges = list(
        (
            await session.scalars(
                select(Relationship)
                .where(
                    Relationship.workspace_id == scope.workspace_id,
                    or_(
                        Relationship.source_entity_id.in_(ids),
                        Relationship.target_entity_id.in_(ids),
                    ),
                )
                .order_by(Relationship.id)
                .limit(101)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(edges) > 100:
        raise ValueError("Entity deletion closure exceeds its atomic relationship limit")
    relationship_ids = [item.id for item in edges]
    supports = (
        list(
            (
                await session.scalars(
                    select(RelationshipEvidence)
                    .where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id.in_(relationship_ids),
                    )
                    .order_by(RelationshipEvidence.id)
                    .limit(201)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if relationship_ids
        else []
    )
    support_snapshot = [
        (
            item.id,
            item.relationship_id,
            item.document_version_id,
            item.chunk_id,
            item.source_membership_id,
            item.target_membership_id,
            item.confidence,
        )
        for item in supports
    ]
    if (
        len(supports) > 200
        or len({(item.document_version_id, item.chunk_id) for item in supports}) > 100
    ):
        raise ValueError("Entity deletion closure exceeds its atomic support limit")
    await lock_relationship_ids(
        session, relationship_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    support_ids = sorted((item.id for item in supports), key=str)
    if support_ids:
        await session.scalars(
            select(RelationshipEvidence.id)
            .where(_evidence_scope(scope), RelationshipEvidence.id.in_(support_ids))
            .order_by(RelationshipEvidence.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    refreshed_edges = list(
        (
            await session.scalars(
                select(Relationship)
                .where(
                    Relationship.workspace_id == scope.workspace_id,
                    or_(
                        Relationship.source_entity_id.in_(ids),
                        Relationship.target_entity_id.in_(ids),
                    ),
                )
                .order_by(Relationship.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    refreshed_supports = (
        list(
            (
                await session.scalars(
                    select(RelationshipEvidence)
                    .where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id.in_(relationship_ids),
                    )
                    .order_by(RelationshipEvidence.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if relationship_ids
        else []
    )
    if (
        [item.id for item in refreshed_edges] != relationship_ids
        or [item.id for item in refreshed_supports] != support_ids
        or support_snapshot
        != [
            (
                item.id,
                item.relationship_id,
                item.document_version_id,
                item.chunk_id,
                item.source_membership_id,
                item.target_membership_id,
                item.confidence,
            )
            for item in refreshed_supports
        ]
    ):
        raise ValueError("Entity deletion relationship closure changed; retry")
    return relationship_ids, support_ids


async def remove_entity_closure(
    session: AsyncSession,
    entity_ids: list[UUID],
    relationship_ids: list[UUID],
    support_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Delete the verified incident relationship closure and its support rows."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(entity_ids), key=str)
    current = list(
        (
            await session.scalars(
                select(Relationship.id)
                .where(
                    Relationship.workspace_id == scope.workspace_id,
                    or_(
                        Relationship.source_entity_id.in_(ids),
                        Relationship.target_entity_id.in_(ids),
                    ),
                )
                .order_by(Relationship.id)
            )
        ).all()
    )
    if current != sorted(set(relationship_ids), key=str):
        raise ValueError("Entity deletion relationship closure changed; retry")
    await session.execute(
        delete(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.id.in_(support_ids),
        )
    )
    await session.execute(
        delete(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.id.in_(relationship_ids),
        )
    )


async def remove_document_support(
    session: AsyncSession,
    closure: RelationshipSupportClosure,
    *,
    refs: list[tuple[UUID, UUID]],
    membership_ids: list[UUID] | tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Remove prepared evidence rows scoped to a document and refresh affected derived edges."""
    if closure.document_id is None:
        raise ValueError("A document closure is required")
    return await _remove_support(
        session,
        closure,
        refs=refs,
        membership_ids=membership_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def remove_source_support(
    session: AsyncSession,
    closure: RelationshipSupportClosure,
    *,
    refs: list[tuple[UUID, UUID]],
    membership_ids: list[UUID] | tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Remove prepared evidence rows scoped to a Source and refresh affected derived edges."""
    if closure.document_id is not None:
        raise ValueError("A Source-wide closure is required")
    return await _remove_support(
        session,
        closure,
        refs=refs,
        membership_ids=membership_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def _remove_support(
    session: AsyncSession,
    closure: RelationshipSupportClosure,
    *,
    refs: list[tuple[UUID, UUID]],
    membership_ids: list[UUID] | tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Delete prepared evidence and remove unsupported derived relationships; never locks.

    Requires ``purge_history_support`` to have run in this transaction: any history row still
    matching ``refs`` means the purge was skipped or raced, so cleanup fails.
    """
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    current = await _discover_support(
        session,
        refs=refs,
        source_id=closure.source_id,
        document_id=closure.document_id,
        membership_ids=tuple(membership_ids),
        scope=scope,
        include_history=True,
    )
    if (
        current.overflow
        or current.history_ids
        or replace(current, history_ids=closure.history_ids) != closure
    ):
        raise RuntimeError("cleanup closure changed")
    if closure.evidence_ids:
        await session.execute(
            delete(RelationshipEvidence).where(
                _evidence_scope(scope),
                RelationshipEvidence.id.in_(closure.evidence_ids),
            )
        )
    for relationship_id in closure.relationship_ids:
        relationship = await session.get(Relationship, relationship_id)
        if (
            relationship is None
            or relationship.workspace_id != scope.workspace_id
            or relationship.origin != "derived"
        ):
            continue
        remaining = list(
            (
                await session.scalars(
                    select(RelationshipEvidence).where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id == relationship_id,
                    )
                )
            ).all()
        )
        if not remaining:
            await session.delete(relationship)
        else:
            relationship.confidence = max(row.confidence for row in remaining)
    return len(closure.evidence_ids)
