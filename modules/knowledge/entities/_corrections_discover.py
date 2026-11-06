"""Closure discovery (owned rows, redirects) for merge/split corrections."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.knowledge.entities._corrections_common import (
    MAX_CORRECTION_ENTITIES,
    MAX_CORRECTION_EVIDENCE_REFS,
    MAX_CORRECTION_MEMBERSHIPS,
    _Closure,
    _conflict,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityRedirect,
)
from modules.knowledge.relationships import public as relationships
from modules.timeline import public as timeline


async def _discover_owned_rows(
    session: AsyncSession,
    ids: list[UUID],
    *,
    scope: Scope,
    include_target_memberships: bool,
) -> tuple[
    list[EntityEvidenceMembership],
    list[EntityAlias],
    list[EntityAliasEvidence],
    list[EntityFieldEvidence],
]:
    """Read the bounded memberships, aliases and derived supports owned by the correction entities.

    Queries run in a fixed order (source memberships, optional target memberships, aliases, alias supports,
    field supports), each limited to ``MAX_CORRECTION_MEMBERSHIPS + 1``; exceeding the bound raises
    ``CorrectionConflictError("correction_too_large")``. Read-only.
    """
    source_memberships = (
        list(
            (
                await session.scalars(
                    select(EntityEvidenceMembership)
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityEvidenceMembership.entity_id == ids[0],
                    )
                    .order_by(EntityEvidenceMembership.id)
                    .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if ids
        else []
    )
    if include_target_memberships and len(ids) > 1:
        target_memberships = list(
            (
                await session.scalars(
                    select(EntityEvidenceMembership)
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityEvidenceMembership.entity_id == ids[1],
                    )
                    .order_by(EntityEvidenceMembership.id)
                    .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        source_memberships.extend(target_memberships)
    if len(source_memberships) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict(
            "correction_too_large", "Correction membership closure exceeds 200", entity_ids=ids
        )
    aliases = (
        list(
            (
                await session.scalars(
                    select(EntityAlias)
                    .join(Entity, Entity.id == EntityAlias.entity_id)
                    .where(
                        Entity.workspace_id == scope.workspace_id,
                        EntityAlias.entity_id.in_(ids),
                    )
                    .order_by(EntityAlias.id)
                    .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if ids
        else []
    )
    if len(aliases) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict(
            "correction_too_large", "Correction alias closure exceeds 200", entity_ids=ids
        )
    alias_supports = (
        list(
            (
                await session.scalars(
                    select(EntityAliasEvidence)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
                    )
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityAliasEvidence.alias_id.in_([item.id for item in aliases]),
                    )
                    .order_by(EntityAliasEvidence.id)
                    .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if aliases
        else []
    )
    field_supports = (
        list(
            (
                await session.scalars(
                    select(EntityFieldEvidence)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                    )
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityFieldEvidence.entity_id.in_(ids),
                    )
                    .order_by(EntityFieldEvidence.id)
                    .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if ids
        else []
    )
    if (
        len(alias_supports) > MAX_CORRECTION_MEMBERSHIPS
        or len(field_supports) > MAX_CORRECTION_MEMBERSHIPS
    ):
        raise _conflict(
            "correction_too_large", "Correction derived support closure exceeds 200", entity_ids=ids
        )
    return source_memberships, aliases, alias_supports, field_supports


async def _discover_redirects(
    session: AsyncSession,
    ids: list[UUID],
    *,
    scope: Scope,
) -> tuple[list[EntityRedirect], set[UUID]]:
    """Walk redirects that point into the correction entities and reject cycles, size excess or retained evidence.

    Returns the redirect rows sorted by old entity id and the set of redirected (old) entity ids. A redirected
    entity that still owns memberships, aliases or field evidence raises ``redirect_dependency_conflict``.
    """
    redirect_rows_by_old_id: dict[UUID, EntityRedirect] = {}
    redirect_frontier = set(ids)
    while redirect_frontier:
        children = list(
            (
                await session.scalars(
                    select(EntityRedirect)
                    .where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                        EntityRedirect.target_entity_id.in_(redirect_frontier),
                    )
                    .order_by(EntityRedirect.old_entity_id)
                    .limit(MAX_CORRECTION_ENTITIES + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(children) > MAX_CORRECTION_ENTITIES:
            raise _conflict(
                "correction_too_large", "Correction redirect closure exceeds 100", entity_ids=ids
            )
        next_frontier: set[UUID] = set()
        for redirect in children:
            if redirect.old_entity_id not in redirect_rows_by_old_id:
                redirect_rows_by_old_id[redirect.old_entity_id] = redirect
                if redirect.old_entity_id not in ids:
                    next_frontier.add(redirect.old_entity_id)
        if len(redirect_rows_by_old_id) > MAX_CORRECTION_ENTITIES:
            raise _conflict(
                "correction_too_large", "Correction redirect closure exceeds 100", entity_ids=ids
            )
        if set(ids) & set(redirect_rows_by_old_id):
            raise _conflict(
                "redirect_cycle", "Correction redirect closure contains a cycle", entity_ids=ids
            )
        redirect_frontier = next_frontier
    redirect_rows = sorted(redirect_rows_by_old_id.values(), key=lambda row: str(row.old_entity_id))
    redirect_entity_ids = set(redirect_rows_by_old_id)
    if redirect_entity_ids:
        retained_memberships = list(
            (
                await session.scalars(
                    select(EntityEvidenceMembership.id)
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityEvidenceMembership.entity_id.in_(redirect_entity_ids),
                    )
                    .order_by(EntityEvidenceMembership.id)
                    .limit(1)
                )
            ).all()
        )
        retained_aliases = list(
            (
                await session.scalars(
                    select(EntityAlias.id)
                    .join(
                        Entity,
                        Entity.id == EntityAlias.entity_id,
                    )
                    .where(
                        Entity.workspace_id == scope.workspace_id,
                        EntityAlias.entity_id.in_(redirect_entity_ids),
                    )
                    .order_by(EntityAlias.id)
                    .limit(1)
                )
            ).all()
        )
        retained_fields = list(
            (
                await session.scalars(
                    select(EntityFieldEvidence.id)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                    )
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityFieldEvidence.entity_id.in_(redirect_entity_ids),
                    )
                    .order_by(EntityFieldEvidence.id)
                    .limit(1)
                )
            ).all()
        )
        if retained_memberships or retained_aliases or retained_fields:
            raise _conflict(
                "redirect_dependency_conflict",
                "Redirect closure contains retained entity-owned evidence that needs reconciliation",
                entity_ids=sorted(redirect_entity_ids, key=str),
                membership_ids=retained_memberships,
            )
    return redirect_rows, redirect_entity_ids


async def _discover(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
    include_target_memberships: bool = False,
) -> _Closure:
    """Read and bound the merge/split support graph before acquiring its locks."""
    ids = sorted(set(entity_ids), key=str)
    entity_rows = (
        list(
            (
                await session.scalars(
                    select(Entity)
                    .where(Entity.workspace_id == scope.workspace_id, Entity.id.in_(ids))
                    .order_by(Entity.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if ids
        else []
    )
    if len(entity_rows) != len(ids):
        raise _conflict("entity_missing", "A correction entity no longer exists", entity_ids=ids)
    source_memberships, aliases, alias_supports, field_supports = await _discover_owned_rows(
        session,
        ids,
        scope=scope,
        include_target_memberships=include_target_memberships,
    )
    owner_membership_ids = {item.id for item in source_memberships}
    support_membership_ids = {item.membership_id for item in alias_supports} | {
        item.membership_id for item in field_supports
    }
    if not support_membership_ids <= owner_membership_ids:
        raise _conflict(
            "correction_closure_changed",
            "Derived support references evidence outside the correction membership closure",
            entity_ids=ids,
        )
    redirect_rows, redirect_entity_ids = await _discover_redirects(session, ids, scope=scope)
    entity_closure_ids = sorted(set(ids) | redirect_entity_ids, key=str)
    if len(entity_closure_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict(
            "correction_too_large",
            "Correction redirect closure exceeds 100 entities",
            entity_ids=entity_closure_ids,
        )
    try:
        relationship_refs = await relationships.list_correction_relationship_refs(
            session,
            entity_closure_ids,
            scope=scope,
        )
    except ValueError as exc:
        raise _conflict("correction_too_large", str(exc), entity_ids=entity_closure_ids) from exc
    closure_entity_ids = sorted(
        set(entity_closure_ids)
        | {
            endpoint
            for item in relationship_refs
            for endpoint in (item.source_entity_id, item.target_entity_id)
        },
        key=str,
    )
    if len(closure_entity_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict(
            "correction_too_large",
            "Correction neighbor closure exceeds 100 entities",
            entity_ids=closure_entity_ids,
        )
    if closure_entity_ids != ids:
        entity_rows = list(
            (
                await session.scalars(
                    select(Entity)
                    .where(
                        Entity.workspace_id == scope.workspace_id,
                        Entity.id.in_(closure_entity_ids),
                    )
                    .order_by(Entity.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(entity_rows) != len(closure_entity_ids):
            raise _conflict(
                "entity_missing",
                "A correction neighbor no longer exists",
                entity_ids=closure_entity_ids,
            )
    pairs = sorted(
        {(row.document_version_id, row.chunk_id) for row in source_memberships}
        | {
            (support.document_version_id, support.chunk_id)
            for ref in relationship_refs
            for support in ref.supports
        },
        key=lambda pair: (str(pair[0]), str(pair[1])),
    )
    relationship_membership_ids = {
        membership_id
        for ref in relationship_refs
        for support in ref.supports
        for membership_id in (support.source_membership_id, support.target_membership_id)
        if membership_id is not None
    }
    all_membership_ids = owner_membership_ids | support_membership_ids | relationship_membership_ids
    if len(pairs) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict(
            "correction_too_large",
            "Correction evidence closure exceeds 100 references",
            entity_ids=closure_entity_ids,
        )
    source_ids = sorted(
        {row.source_id for row in source_memberships}
        | {
            support.source_id
            for ref in relationship_refs
            for support in ref.supports
            if support.source_id is not None
        }
        | {row.source_id for row in aliases if row.source_id is not None},
        key=str,
    )
    document_ids = sorted(
        {row.document_id for row in source_memberships}
        | {
            support.document_id
            for ref in relationship_refs
            for support in ref.supports
            if support.document_id is not None
        },
        key=str,
    )
    if len(document_ids) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict(
            "correction_too_large",
            "Correction document lock closure exceeds 100",
            entity_ids=closure_entity_ids,
        )
    try:
        timeline_event_ids = await timeline.correction_event_ids(
            session, closure_entity_ids, scope=scope
        )
    except ValueError as exc:
        raise _conflict("correction_too_large", str(exc), entity_ids=closure_entity_ids) from exc
    return _Closure(
        entity_rows,
        source_memberships,
        aliases,
        alias_supports,
        field_supports,
        relationship_refs,
        redirect_rows,
        entity_closure_ids,
        closure_entity_ids,
        pairs,
        source_ids,
        document_ids,
        sorted(all_membership_ids, key=str),
        timeline_event_ids,
    )
