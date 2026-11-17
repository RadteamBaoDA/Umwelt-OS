"""Entity support cleanup closure, removal and changed-entity reads.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityEvidenceMembership,
    EntityFieldEvidence,
)
from modules.sources.public import SourceFence


@dataclass(frozen=True)
class EntitySupportClosure:
    """Workspace-qualified ID closure of entity support for one Source or Document."""

    source_id: UUID
    document_id: UUID | None
    membership_ids: tuple[UUID, ...]
    alias_ids: tuple[UUID, ...]
    alias_evidence_ids: tuple[UUID, ...]
    field_evidence_ids: tuple[UUID, ...]
    entity_ids: tuple[UUID, ...]
    overflow: bool


_CLEANUP_LIMIT = 10_000


def _require_cleanup_fences(
    actual: AccessFence,
    *,
    source_id: UUID,
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
        or source_fence.id != source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise HTTPException(status_code=409, detail="Cleanup authority changed")


async def _membership_alias_field_support(
    session: AsyncSession, workspace_id: UUID, membership_ids: list[UUID]
) -> tuple[set[UUID], list[UUID], list[UUID], bool]:
    """Read alias and field evidence ids attached to the given memberships (ID-only, nonlocking).

    Each read is capped at ``_CLEANUP_LIMIT``; returns ``(alias_ids, alias_evidence_ids,
    field_evidence_ids, overflow)`` where ``overflow`` is true when either read had more rows.

    """
    overflow = False
    alias_ids: set[UUID] = set()
    alias_evidence_ids: list[UUID] = []
    field_evidence_ids: list[UUID] = []
    if membership_ids:
        alias_support = list(
            (
                await session.execute(
                    select(EntityAliasEvidence.id, EntityAliasEvidence.alias_id)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
                    )
                    .where(
                        EntityEvidenceMembership.workspace_id == workspace_id,
                        EntityAliasEvidence.membership_id.in_(membership_ids),
                    )
                    .order_by(EntityAliasEvidence.id)
                    .limit(_CLEANUP_LIMIT + 1)
                )
            ).all()
        )
        overflow |= len(alias_support) > _CLEANUP_LIMIT
        alias_support = alias_support[:_CLEANUP_LIMIT]
        alias_evidence_ids = [row[0] for row in alias_support]
        alias_ids.update(row[1] for row in alias_support)
        field_evidence_ids = list(
            (
                await session.scalars(
                    select(EntityFieldEvidence.id)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                    )
                    .where(
                        EntityEvidenceMembership.workspace_id == workspace_id,
                        EntityFieldEvidence.membership_id.in_(membership_ids),
                    )
                    .order_by(EntityFieldEvidence.id)
                    .limit(_CLEANUP_LIMIT + 1)
                )
            ).all()
        )
        overflow |= len(field_evidence_ids) > _CLEANUP_LIMIT
        field_evidence_ids = field_evidence_ids[:_CLEANUP_LIMIT]
    return alias_ids, alias_evidence_ids, field_evidence_ids, overflow


async def support_cleanup_ids(
    session: AsyncSession,
    *,
    source_id: UUID,
    document_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntitySupportClosure:
    """Discover (nonlocking, ID-only) the entity support rows of one Source or Document."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    workspace_id = scope.workspace_id
    overflow = False
    member_where = [
        EntityEvidenceMembership.workspace_id == workspace_id,
        EntityEvidenceMembership.source_id == source_id,
    ]
    if document_id is not None:
        member_where.append(EntityEvidenceMembership.document_id == document_id)
    members = list(
        (
            await session.execute(
                select(EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id)
                .where(*member_where)
                .order_by(EntityEvidenceMembership.id)
                .limit(_CLEANUP_LIMIT + 1)
            )
        ).all()
    )
    overflow |= len(members) > _CLEANUP_LIMIT
    members = members[:_CLEANUP_LIMIT]
    membership_ids = [row[0] for row in members]
    entity_ids = {row[1] for row in members}
    (
        alias_ids,
        alias_evidence_ids,
        field_evidence_ids,
        support_overflow,
    ) = await _membership_alias_field_support(session, workspace_id, membership_ids)
    overflow |= support_overflow
    alias_clauses = [EntityAlias.source_id == source_id] if document_id is None else []
    if alias_ids:
        alias_clauses.append(EntityAlias.id.in_(alias_ids))
    if alias_clauses:
        aliases = list(
            (
                await session.execute(
                    select(EntityAlias.id, EntityAlias.entity_id)
                    .join(Entity, Entity.id == EntityAlias.entity_id)
                    .where(Entity.workspace_id == workspace_id, or_(*alias_clauses))
                    .order_by(EntityAlias.id)
                    .limit(_CLEANUP_LIMIT + 1)
                )
            ).all()
        )
        overflow |= len(aliases) > _CLEANUP_LIMIT
        for alias_id, alias_entity_id in aliases[:_CLEANUP_LIMIT]:
            alias_ids.add(alias_id)
            entity_ids.add(alias_entity_id)
    return EntitySupportClosure(
        source_id=source_id,
        document_id=document_id,
        membership_ids=tuple(sorted(membership_ids)),
        alias_ids=tuple(sorted(alias_ids)),
        alias_evidence_ids=tuple(sorted(alias_evidence_ids)),
        field_evidence_ids=tuple(sorted(field_evidence_ids)),
        entity_ids=tuple(sorted(entity_ids)),
        overflow=overflow,
    )


async def prepare_support_cleanup_in_uow(
    session: AsyncSession,
    closure: EntitySupportClosure,
    *,
    entity_ids: tuple[UUID, ...],
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Lock the combined entity union, then support children, in UUID order; no mutation."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        source_id=closure.source_id,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if closure.overflow:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    workspace_id = scope.workspace_id
    locked_entities = sorted(set(entity_ids) | set(closure.entity_ids))
    workspace_entities = select(Entity.id).where(Entity.workspace_id == workspace_id)
    workspace_memberships = select(EntityEvidenceMembership.id).where(
        EntityEvidenceMembership.workspace_id == workspace_id,
    )
    if locked_entities:
        await session.scalars(
            select(Entity.id)
            .where(Entity.workspace_id == workspace_id, Entity.id.in_(locked_entities))
            .order_by(Entity.id)
            .with_for_update()
        )
    if closure.membership_ids:
        await session.scalars(
            select(EntityEvidenceMembership.id)
            .where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityEvidenceMembership.id.in_(closure.membership_ids),
            )
            .order_by(EntityEvidenceMembership.id)
            .with_for_update()
        )
    if closure.alias_ids:
        await session.scalars(
            select(EntityAlias.id)
            .where(
                EntityAlias.entity_id.in_(workspace_entities),
                EntityAlias.id.in_(closure.alias_ids),
            )
            .order_by(EntityAlias.id)
            .with_for_update()
        )
    if closure.alias_evidence_ids:
        await session.scalars(
            select(EntityAliasEvidence.id)
            .where(
                EntityAliasEvidence.membership_id.in_(workspace_memberships),
                EntityAliasEvidence.id.in_(closure.alias_evidence_ids),
            )
            .order_by(EntityAliasEvidence.id)
            .with_for_update()
        )
    if closure.field_evidence_ids:
        await session.scalars(
            select(EntityFieldEvidence.id)
            .where(
                EntityFieldEvidence.membership_id.in_(workspace_memberships),
                EntityFieldEvidence.id.in_(closure.field_evidence_ids),
            )
            .order_by(EntityFieldEvidence.id)
            .with_for_update()
        )


async def lock_entity_ids(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Lock a bounded sorted set of entity rows for support cleanup."""
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 10_000:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if ids:
        await session.scalars(
            select(Entity.id)
            .where(
                Entity.workspace_id == scope.workspace_id,
                Entity.id.in_(ids),
            )
            .order_by(Entity.id)
            .with_for_update()
        )


async def remove_document_support(
    session: AsyncSession,
    closure: EntitySupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Remove prepared evidence memberships and unsupported derived fields for one document."""
    if closure.document_id is None:
        raise ValueError("A document closure is required")
    return await _remove_entity_support(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def remove_source_support(
    session: AsyncSession,
    closure: EntitySupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Remove prepared evidence memberships and unsupported derived fields for one Source."""
    if closure.document_id is not None:
        raise ValueError("A Source-wide closure is required")
    return await _remove_entity_support(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def _remove_entity_support(
    session: AsyncSession,
    closure: EntitySupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> int:
    """Delete prepared evidence and aliases, preserving owner aliases and supported values.

    Rows were locked by ``prepare_support_cleanup_in_uow``; this never locks.
    """
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        source_id=closure.source_id,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    current = await support_cleanup_ids(
        session,
        source_id=closure.source_id,
        document_id=closure.document_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current.overflow or current != closure:
        raise RuntimeError("cleanup closure changed")
    workspace_id = scope.workspace_id
    source_wide = closure.document_id is None
    if closure.alias_evidence_ids:
        await session.execute(
            delete(EntityAliasEvidence).where(
                EntityAliasEvidence.id.in_(closure.alias_evidence_ids),
                EntityAliasEvidence.membership_id.in_(
                    select(EntityEvidenceMembership.id).where(
                        EntityEvidenceMembership.workspace_id == workspace_id
                    )
                ),
            )
        )
    if closure.membership_ids:
        await session.execute(
            delete(EntityEvidenceMembership).where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityEvidenceMembership.id.in_(closure.membership_ids),
            )
        )
    for alias_id in closure.alias_ids:
        alias = await session.get(EntityAlias, alias_id)
        if alias is None:
            continue
        if source_wide and alias.source_id == closure.source_id:
            alias.source_id = None
        remaining_confidence = await session.scalar(
            select(func.max(EntityAliasEvidence.confidence))
            .join(
                EntityEvidenceMembership,
                EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
            )
            .where(
                EntityAliasEvidence.alias_id == alias.id,
                EntityEvidenceMembership.entity_id == alias.entity_id,
                EntityEvidenceMembership.workspace_id == workspace_id,
            )
        )
        if alias.origin == "owner":
            continue
        if remaining_confidence is not None:
            alias.confidence = remaining_confidence
            continue
        # Origin-less legacy aliases carry no proof of owner authorship. Forget
        # them with their final exact support instead of upgrading their status.
        await session.delete(alias)
    await _clear_unsupported_derived_fields(
        session, set(closure.entity_ids), workspace_id=workspace_id
    )
    return len(closure.membership_ids)


async def _clear_unsupported_derived_fields(
    session: AsyncSession,
    entity_ids: set[UUID],
    *,
    workspace_id: UUID,
) -> None:
    """Clear non-owner entity fields whose exact current evidence support was removed."""
    for entity_id in sorted(entity_ids):
        entity = await session.get(Entity, entity_id)
        if entity is not None and entity.workspace_id == workspace_id:
            changed = False
            # Unknown legacy provenance is not proof that a non-owner value is safe to retain.
            if entity.name_origin != "owner" and entity.name is not None:
                name_hash = sha256(entity.name.encode("utf-8")).hexdigest()
                name_supported = (
                    entity.name_origin == "derived"
                    and await session.scalar(
                        select(EntityFieldEvidence.id)
                        .join(
                            EntityEvidenceMembership,
                            EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                        )
                        .where(
                            EntityFieldEvidence.entity_id == entity_id,
                            EntityFieldEvidence.field_name == "name",
                            EntityFieldEvidence.value_hash == name_hash,
                            EntityEvidenceMembership.entity_id == entity_id,
                            EntityEvidenceMembership.workspace_id == workspace_id,
                        )
                        .limit(1)
                    )
                    is not None
                )
                if not name_supported:
                    entity.name = None
                    entity.canonical_name = None
                    entity.name_origin = None
                    changed = True
            elif entity.name is None and entity.name_origin != "owner":
                changed = entity.canonical_name is not None or changed
                entity.canonical_name = None
                entity.name_origin = None
            if entity.description_origin != "owner" and entity.description is not None:
                description_hash = sha256(entity.description.encode("utf-8")).hexdigest()
                description_supported = (
                    entity.description_origin == "derived"
                    and await session.scalar(
                        select(EntityFieldEvidence.id)
                        .join(
                            EntityEvidenceMembership,
                            EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                        )
                        .where(
                            EntityFieldEvidence.entity_id == entity_id,
                            EntityFieldEvidence.field_name == "description",
                            EntityFieldEvidence.value_hash == description_hash,
                            EntityEvidenceMembership.entity_id == entity_id,
                            EntityEvidenceMembership.workspace_id == workspace_id,
                        )
                        .limit(1)
                    )
                    is not None
                )
                if not description_supported:
                    entity.description = None
                    entity.description_origin = None
                    changed = True
            elif entity.description is None and entity.description_origin != "owner":
                entity.description_origin = None
            if changed:
                entity.revision += 1


async def list_changed_entities_after(
    session: AsyncSession,
    position: tuple[datetime, UUID] | None,
    limit: int = 100,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, str]]]:
    """Read-only cursor page of entities by ``(updated_at, id)`` for the automations sweep.

    The key combines id and ``updated_at`` so every change is a distinct trigger event. Payload
    carries id, type and a created/updated marker only.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    stmt = select(Entity).where(Entity.workspace_id == scope.workspace_id)
    if position is not None:
        stmt = stmt.where(tuple_(Entity.updated_at, Entity.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(Entity.updated_at, Entity.id).limit(limit))).all()
    return [
        (
            r.updated_at,
            r.id,
            f"{r.id}:{r.updated_at.isoformat()}",
            {
                "entity_id": str(r.id),
                "entity_type": r.type,
                "change": "created" if r.revision == 1 else "updated",
            },
        )
        for r in rows
    ]
