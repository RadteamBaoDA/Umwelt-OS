"""Entity correction closure reads and merge/split application for relationships.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.workspaces.schemas import (
    Scope,
    WorkspaceContext,
)
from modules.knowledge.relationships._common import (
    _admit,
    _evidence_scope,
)
from modules.knowledge.relationships._history import (
    record_relationship_history,
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
)
from modules.knowledge.relationships.schemas import (
    CorrectionRelationshipRef,
    CorrectionSupportRef,
)


async def list_correction_relationship_refs(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
) -> list[CorrectionRelationshipRef]:
    """Read a bounded incident edge/support snapshot in one workspace for correction planning.

    The calling correction transaction already holds owner admission; this read adds no
    authorization of its own beyond the role-only owner guard, and restricts rows to the scoped workspace.
    """
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Correction entity closure exceeds its atomic limit")
    if not ids:
        return []
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
        raise ValueError("Correction relationship closure exceeds its atomic limit")
    if len(edges) != len(set(entity_ids)) and any(
        edge.source_entity_id == edge.target_entity_id for edge in edges
    ):
        raise ValueError("Correction relationship closure contains a self relationship")
    supports = (
        (
            await session.scalars(
                select(RelationshipEvidence)
                .where(
                    _evidence_scope(scope),
                    RelationshipEvidence.relationship_id.in_([edge.id for edge in edges]),
                )
                .order_by(RelationshipEvidence.relationship_id, RelationshipEvidence.id)
                .limit(201)
                .execution_options(populate_existing=True)
            )
        ).all()
        if edges
        else []
    )
    if (
        len(supports) > 200
        or len({(row.document_version_id, row.chunk_id) for row in supports}) > 100
    ):
        raise ValueError("Correction evidence closure exceeds its atomic limit")
    if (
        len(
            {
                identifier
                for row in supports
                for identifier in (row.source_membership_id, row.target_membership_id)
                if identifier is not None
            }
        )
        > 200
    ):
        raise ValueError("Correction membership closure exceeds its atomic limit")
    by_edge: dict[UUID, list[CorrectionSupportRef]] = {}
    for row in supports:
        by_edge.setdefault(row.relationship_id, []).append(
            CorrectionSupportRef(
                id=row.id,
                document_id=row.document_id,
                source_id=row.source_id,
                document_version_id=row.document_version_id,
                chunk_id=row.chunk_id,
                source_membership_id=row.source_membership_id,
                target_membership_id=row.target_membership_id,
                confidence=row.confidence,
            )
        )
    return [
        CorrectionRelationshipRef(
            id=edge.id,
            source_entity_id=edge.source_entity_id,
            target_entity_id=edge.target_entity_id,
            type=edge.type,
            origin=edge.origin,
            valid_from=edge.valid_from,
            valid_to=edge.valid_to,
            metadata=edge.metadata_json,
            supports=by_edge.get(edge.id, []),
        )
        for edge in edges
    ]


async def lock_correction_closure(
    session: AsyncSession,
    entity_ids: list[UUID],
    expected_relationship_ids: set[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Lock and revalidate the relationship/support snapshot before correction writes."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, entity_ids, scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    relation_ids = sorted(expected_relationship_ids, key=str)
    if relation_ids:
        await session.scalars(
            select(Relationship)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id.in_(relation_ids),
            )
            .order_by(Relationship.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        support_ids = sorted({support.id for ref in refs for support in ref.supports}, key=str)
        if support_ids:
            await session.scalars(
                select(RelationshipEvidence)
                .where(
                    _evidence_scope(scope),
                    RelationshipEvidence.id.in_(support_ids),
                )
                .order_by(RelationshipEvidence.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        refreshed = await list_correction_relationship_refs(session, entity_ids, scope=scope)
        if [item.model_dump(mode="json") for item in refs] != [
            item.model_dump(mode="json") for item in refreshed
        ]:
            raise ValueError("Correction relationship support closure changed; retry preview")


def validate_entity_merge_plan(
    source_entity_id: UUID,
    target_entity_id: UUID,
    refs: list[CorrectionRelationshipRef],
    source_redirect_ids: set[UUID] | None = None,
) -> None:
    """Reject merge plans that create self-edges or conflicting metadata."""
    source_ids = {source_entity_id, *(source_redirect_ids or set())}
    groups: dict[tuple[object, ...], list[CorrectionRelationshipRef]] = {}
    for ref in refs:
        if ref.origin != "derived" and ref.supports:
            raise ValueError(
                "Merge has unexpected evidence attached to an owner-authored relationship"
            )
        source_id = target_entity_id if ref.source_entity_id in source_ids else ref.source_entity_id
        target_id = target_entity_id if ref.target_entity_id in source_ids else ref.target_entity_id
        if source_id == target_id:
            raise ValueError("Merge would create a self relationship")
        key = (source_id, target_id, ref.type, ref.origin, ref.valid_from, ref.valid_to)
        groups.setdefault(key, []).append(ref)
    for group in groups.values():
        if any(ref.metadata != group[0].metadata for ref in group[1:]):
            raise ValueError("Merge has conflicting relationship metadata")


def validate_entity_split_plan(
    entity_id: UUID,
    membership_ids: set[UUID],
    refs: list[CorrectionRelationshipRef],
) -> None:
    """Reject split plans with unresolved endpoint support or self-edges."""
    for ref in refs:
        if ref.origin == "owner" and ref.supports:
            raise ValueError(
                "Split has unexpected evidence attached to an owner-authored relationship"
            )
        if ref.supports and any(
            support.source_membership_id is None or support.target_membership_id is None
            for support in ref.supports
        ):
            raise ValueError("Split has unresolved legacy relationship endpoint bindings")
        for support in ref.supports:
            moves_source = support.source_membership_id in membership_ids
            moves_target = support.target_membership_id in membership_ids
            if moves_source and moves_target:
                raise ValueError("Split would create a self relationship")


async def apply_entity_merge(
    session: AsyncSession,
    source_entity_id: UUID,
    target_entity_id: UUID,
    expected_relationship_ids: set[UUID],
    closure_entity_ids: list[UUID],
    source_redirect_ids: set[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID]]:
    """Redirect incident edges and retain presently observed post-correction history.

    Caller fences and captures old history before membership moves, then commits
    correction scheduling. This owner helper flushes; it never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, closure_entity_ids, scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    validate_entity_merge_plan(source_entity_id, target_entity_id, refs, source_redirect_ids)
    relation_ids = sorted(expected_relationship_ids, key=str)
    rows = (
        list(
            (
                await session.scalars(
                    select(Relationship)
                    .where(
                        Relationship.workspace_id == scope.workspace_id,
                        Relationship.id.in_(relation_ids),
                    )
                    .order_by(Relationship.id)
                )
            ).all()
        )
        if relation_ids
        else []
    )
    source_merge_ids = {source_entity_id} | source_redirect_ids
    key_groups: dict[
        tuple[UUID, UUID, str, str, datetime | None, datetime | None], list[Relationship]
    ] = {}
    for row in rows:
        next_source = (
            target_entity_id
            if row.source_entity_id in ({source_entity_id} | source_redirect_ids)
            else row.source_entity_id
        )
        next_target = (
            target_entity_id
            if row.target_entity_id in ({source_entity_id} | source_redirect_ids)
            else row.target_entity_id
        )
        key = (next_source, next_target, row.type, row.origin, row.valid_from, row.valid_to)
        key_groups.setdefault(key, []).append(row)
    replacements: list[tuple[UUID, UUID]] = []
    affected_relationship_ids: set[UUID] = set()
    for (source_id, target_id, _, _, _, _), group in key_groups.items():
        group.sort(
            key=lambda row: (
                bool(source_merge_ids.intersection((row.source_entity_id, row.target_entity_id))),
                str(row.id),
            )
        )
        survivor = group[0]
        survivor.source_entity_id, survivor.target_entity_id = source_id, target_id
        affected_relationship_ids.add(survivor.id)
        supports_by_key: dict[
            tuple[UUID, UUID, UUID | None, UUID | None], RelationshipEvidence
        ] = {}
        for row in group:
            support_rows = (
                await session.scalars(
                    select(RelationshipEvidence)
                    .where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id == row.id,
                    )
                    .order_by(RelationshipEvidence.id)
                )
            ).all()
            for support in support_rows:
                support_key = (
                    support.document_version_id,
                    support.chunk_id,
                    support.source_membership_id,
                    support.target_membership_id,
                )
                if support_key in supports_by_key:
                    keep = supports_by_key[support_key]
                    keep.confidence = max(keep.confidence, support.confidence)
                    await session.delete(support)
                else:
                    supports_by_key[support_key] = support
                    support.relationship_id = survivor.id
            if row.id != survivor.id:
                replacements.append((row.id, survivor.id))
                await session.delete(row)
    await _refresh_derived_confidence(session, affected_relationship_ids, scope=scope)
    await session.flush()
    for identifier in sorted(affected_relationship_ids):
        await record_relationship_history(
            session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    for old_id, _ in replacements:
        session.add(
            RelationshipSnapshotHistory(
                workspace_id=scope.workspace_id,
                relationship_id=old_id,
                deleted=True,
                state={},
                support=[],
            )
        )
    return replacements


async def _split_row_supports(
    session: AsyncSession,
    row: Relationship,
    source_rows: list[RelationshipEvidence],
    membership_ids: set[UUID],
    new_entity_id: UUID,
    *,
    scope: Scope,
) -> list[tuple[UUID, UUID]]:
    """Re-home the supports of ``row`` that rest on moved memberships onto (possibly new) destination edges.

    Groups moving supports by their new endpoint pair, reuses an equal existing edge (same type, origin and
    validity; differing metadata raises ``ValueError``) or creates one, and deletes ``row`` when every support
    moved. Returns ``(old_id, destination_id)`` per destination; empty when nothing moves. Flushes new edges.
    """
    moving = [
        item
        for item in source_rows
        if item.source_membership_id in membership_ids
        or item.target_membership_id in membership_ids
    ]
    if not moving:
        return []
    endpoints: dict[tuple[UUID, UUID], list[RelationshipEvidence]] = {}
    for support in moving:
        next_source = (
            new_entity_id
            if support.source_membership_id in membership_ids
            else row.source_entity_id
        )
        next_target = (
            new_entity_id
            if support.target_membership_id in membership_ids
            else row.target_entity_id
        )
        if next_source == next_target:
            raise ValueError("Split would create a self relationship")
        endpoints.setdefault((next_source, next_target), []).append(support)
    replaced: list[tuple[UUID, UUID]] = []
    for (next_source, next_target), moved_supports in endpoints.items():
        existing = await session.scalar(
            select(Relationship)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id != row.id,
                Relationship.source_entity_id == next_source,
                Relationship.target_entity_id == next_target,
                Relationship.type == row.type,
                Relationship.origin == row.origin,
                Relationship.valid_from.is_not_distinct_from(row.valid_from),
                Relationship.valid_to.is_not_distinct_from(row.valid_to),
            )
            .order_by(Relationship.id)
            .limit(1)
        )
        if existing is not None and existing.metadata_json != row.metadata_json:
            raise ValueError("Split has conflicting relationship metadata")
        destination = existing or Relationship(
            workspace_id=scope.workspace_id,
            source_entity_id=next_source,
            target_entity_id=next_target,
            type=row.type,
            origin=row.origin,
            confidence=None,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            metadata_json=row.metadata_json,
        )
        if existing is None:
            session.add(destination)
            await session.flush()
        for support in moved_supports:
            support.relationship_id = destination.id
        replaced.append((row.id, destination.id))
    if len(source_rows) == len(moving):
        await session.delete(row)
    return replaced


async def apply_entity_split(
    session: AsyncSession,
    entity_id: UUID,
    new_entity_id: UUID,
    membership_ids: set[UUID],
    expected_relationship_ids: set[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID]]:
    """Move exact endpoint support and retain observed post-correction history.

    Caller captures old history before memberships move and owns correction
    scheduling/commit. Missing old identities receive identifier-only tombstones.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, [entity_id], scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    validate_entity_split_plan(entity_id, membership_ids, refs)
    relation_ids = sorted(expected_relationship_ids, key=str)
    rows = (
        list(
            (
                await session.scalars(
                    select(Relationship)
                    .where(
                        Relationship.workspace_id == scope.workspace_id,
                        Relationship.id.in_(relation_ids),
                    )
                    .order_by(Relationship.id)
                )
            ).all()
        )
        if relation_ids
        else []
    )
    support_by_edge: dict[UUID, list[RelationshipEvidence]] = {}
    for support in (
        (
            await session.scalars(
                select(RelationshipEvidence)
                .where(
                    _evidence_scope(scope),
                    RelationshipEvidence.relationship_id.in_(relation_ids),
                )
                .order_by(RelationshipEvidence.relationship_id, RelationshipEvidence.id)
            )
        ).all()
        if relation_ids
        else []
    ):
        support_by_edge.setdefault(support.relationship_id, []).append(support)
    replacements: list[tuple[UUID, UUID]] = []
    affected_relationship_ids: set[UUID] = set()
    for row in rows:
        source_rows = support_by_edge.get(row.id, [])
        if not source_rows:
            continue
        if row.origin == "owner":
            raise ValueError("Split cannot move evidence from an owner-authored relationship")
        for old_id, destination_id in await _split_row_supports(
            session,
            row,
            source_rows,
            membership_ids,
            new_entity_id,
            scope=scope,
        ):
            affected_relationship_ids.update((old_id, destination_id))
            replacements.append((old_id, destination_id))
    await _refresh_derived_confidence(session, affected_relationship_ids, scope=scope)
    await session.flush()
    for identifier in sorted(affected_relationship_ids):
        if (
            await session.scalar(
                select(Relationship.id).where(
                    Relationship.workspace_id == scope.workspace_id,
                    Relationship.id == identifier,
                )
            )
            is None
        ):
            session.add(
                RelationshipSnapshotHistory(
                    workspace_id=scope.workspace_id,
                    relationship_id=identifier,
                    deleted=True,
                    state={},
                    support=[],
                )
            )
        else:
            await record_relationship_history(
                session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
    return replacements


async def _refresh_derived_confidence(
    session: AsyncSession,
    relationship_ids: set[UUID],
    *,
    scope: Scope,
) -> None:
    """Recompute derived relationship confidence from remaining evidence rows."""
    if not relationship_ids:
        return
    await session.flush()
    for relationship_id in sorted(relationship_ids, key=str):
        relationship = await session.scalar(
            select(Relationship)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id == relationship_id,
            )
            .execution_options(populate_existing=True)
        )
        if relationship is None or relationship.origin != "derived":
            continue
        relationship.confidence = await session.scalar(
            select(func.max(RelationshipEvidence.confidence)).where(
                _evidence_scope(scope),
                RelationshipEvidence.relationship_id == relationship_id,
            )
        )
