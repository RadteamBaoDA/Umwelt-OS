"""Canonical entity deletion: closure discovery, locking and row removal."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import ReplayDraft, commit_with_replay, make_graph_change
from core.workspaces.schemas import AccessFence, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._corrections_common import (
    MAX_CORRECTION_ENTITIES,
    MAX_CORRECTION_EVIDENCE_REFS,
    MAX_CORRECTION_MEMBERSHIPS,
    _admit,
    _conflict,
    _DeleteClosure,
    _temporal_before_relationships,
    _temporal_correction,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityRedirect,
)
from modules.knowledge.relationships import public as relationships
from modules.sources import public as sources
from modules.timeline import public as timeline


async def _delete_redirect_closure(
    session: AsyncSession,
    entity_id: UUID,
    *,
    scope: Scope,
) -> tuple[set[UUID], dict[UUID, EntityRedirect]]:
    """Walk redirects (breadth-first, <= 100 entities) that point into the deletion root.

    Returns the closure entity ids (root included) and the redirect rows keyed by old id. Raises
    ``entity_missing`` when the root was already deleted (cleared stub) and ``entity_redirected`` when it is
    itself redirected; oversized closures raise ``deletion_too_large``.
    """
    entity_ids = {entity_id}
    frontier = {entity_id}
    redirect_rows: dict[UUID, EntityRedirect] = {}
    while frontier:
        children = list(
            (
                await session.scalars(
                    select(EntityRedirect)
                    .where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                        EntityRedirect.target_entity_id.in_(frontier),
                    )
                    .order_by(EntityRedirect.old_entity_id)
                    .limit(MAX_CORRECTION_ENTITIES + 1)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(children) > MAX_CORRECTION_ENTITIES:
            raise _conflict(
                "deletion_too_large",
                "Entity redirect closure exceeds 100",
                entity_ids=sorted(entity_ids, key=str),
            )
        next_frontier: set[UUID] = set()
        for redirect in children:
            if redirect.old_entity_id not in entity_ids:
                entity_ids.add(redirect.old_entity_id)
                next_frontier.add(redirect.old_entity_id)
            redirect_rows[redirect.old_entity_id] = redirect
        if len(entity_ids) > MAX_CORRECTION_ENTITIES:
            raise _conflict(
                "deletion_too_large",
                "Entity redirect closure exceeds 100",
                entity_ids=sorted(entity_ids, key=str),
            )
        frontier = next_frontier
    redirect_rows.update(
        {
            item.old_entity_id: item
            for item in (
                await session.scalars(
                    select(EntityRedirect)
                    .where(
                        EntityRedirect.workspace_id == scope.workspace_id,
                        EntityRedirect.old_entity_id.in_(entity_ids),
                    )
                    .order_by(EntityRedirect.old_entity_id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
    )
    if entity_id in redirect_rows:
        if redirect_rows[entity_id].target_entity_id is None:
            raise _conflict("entity_missing", "Entity was deleted", entity_ids=[entity_id])
        raise _conflict(
            "entity_redirected", "Only canonical entities can be deleted", entity_ids=[entity_id]
        )
    return entity_ids, redirect_rows


async def _delete_owned_rows(
    session: AsyncSession,
    entity_ids: set[UUID],
    *,
    scope: Scope,
) -> tuple[
    list[Entity],
    list[EntityEvidenceMembership],
    list[EntityAlias],
    list[EntityCorrectionDecision],
    list[EntityAliasEvidence],
    list[EntityFieldEvidence],
]:
    """Read the entity rows and bounded owned memberships, aliases, decisions and derived supports for deletion.

    Raises ``deletion_closure_changed`` if a redirect entity vanished and ``deletion_too_large`` when any
    owned or support set exceeds 200 rows. Read-only; query order is fixed.
    """
    entity_rows = list(
        (
            await session.scalars(
                select(Entity)
                .where(
                    Entity.workspace_id == scope.workspace_id,
                    Entity.id.in_(entity_ids),
                )
                .order_by(Entity.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(entity_rows) != len(entity_ids):
        raise _conflict(
            "deletion_closure_changed",
            "Redirect entity disappeared during deletion discovery",
            entity_ids=sorted(entity_ids, key=str),
        )
    memberships = list(
        (
            await session.scalars(
                select(EntityEvidenceMembership)
                .where(
                    EntityEvidenceMembership.workspace_id == scope.workspace_id,
                    EntityEvidenceMembership.entity_id.in_(entity_ids),
                )
                .order_by(EntityEvidenceMembership.id)
                .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    aliases = list(
        (
            await session.scalars(
                select(EntityAlias)
                .join(Entity, Entity.id == EntityAlias.entity_id)
                .where(
                    Entity.workspace_id == scope.workspace_id, EntityAlias.entity_id.in_(entity_ids)
                )
                .order_by(EntityAlias.id)
                .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    decisions = list(
        (
            await session.scalars(
                select(EntityCorrectionDecision)
                .where(
                    EntityCorrectionDecision.workspace_id == scope.workspace_id,
                    or_(
                        EntityCorrectionDecision.entity_id.in_(entity_ids),
                        EntityCorrectionDecision.membership_id.in_(
                            select(EntityEvidenceMembership.id).where(
                                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                                EntityEvidenceMembership.entity_id.in_(entity_ids),
                            )
                        ),
                    ),
                )
                .order_by(EntityCorrectionDecision.id)
                .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if (
        len(memberships) > MAX_CORRECTION_MEMBERSHIPS
        or len(aliases) > MAX_CORRECTION_MEMBERSHIPS
        or len(decisions) > MAX_CORRECTION_MEMBERSHIPS
    ):
        raise _conflict(
            "deletion_too_large",
            "Entity deletion closure exceeds 200 owned rows",
            entity_ids=sorted(entity_ids, key=str),
        )
    alias_supports = (
        list(
            (
                await session.scalars(
                    select(EntityAliasEvidence)
                    .where(
                        EntityAliasEvidence.alias_id.in_([item.id for item in aliases]),
                        EntityAliasEvidence.membership_id.in_(
                            select(EntityEvidenceMembership.id).where(
                                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                            )
                        ),
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
    field_supports = list(
        (
            await session.scalars(
                select(EntityFieldEvidence)
                .where(
                    EntityFieldEvidence.entity_id.in_(entity_ids),
                    EntityFieldEvidence.membership_id.in_(
                        select(EntityEvidenceMembership.id).where(
                            EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        )
                    ),
                )
                .order_by(EntityFieldEvidence.id)
                .limit(MAX_CORRECTION_MEMBERSHIPS + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if (
        len(alias_supports) > MAX_CORRECTION_MEMBERSHIPS
        or len(field_supports) > MAX_CORRECTION_MEMBERSHIPS
    ):
        raise _conflict(
            "deletion_too_large",
            "Entity deletion support closure exceeds 200",
            entity_ids=sorted(entity_ids, key=str),
        )
    return entity_rows, memberships, aliases, decisions, alias_supports, field_supports


async def _discover_delete_closure(
    session: AsyncSession, entity_id: UUID, *, scope: Scope
) -> _DeleteClosure:
    """Discover bounded redirect, evidence, alias, decision, and incident-edge rows."""
    root = await session.scalar(
        select(Entity)
        .where(
            Entity.id == entity_id,
            Entity.workspace_id == scope.workspace_id,
        )
        .execution_options(populate_existing=True)
    )
    if root is None:
        raise _conflict("entity_missing", "Entity no longer exists", entity_ids=[entity_id])
    entity_ids, redirect_rows = await _delete_redirect_closure(session, entity_id, scope=scope)
    (
        entity_rows,
        memberships,
        aliases,
        decisions,
        alias_supports,
        field_supports,
    ) = await _delete_owned_rows(
        session,
        entity_ids,
        scope=scope,
    )
    try:
        relationship_refs = await relationships.list_correction_relationship_refs(
            session,
            sorted(entity_ids, key=str),
            scope=scope,
        )
    except ValueError as exc:
        raise _conflict(
            "deletion_too_large", str(exc), entity_ids=sorted(entity_ids, key=str)
        ) from exc
    neighbor_ids = {
        endpoint
        for item in relationship_refs
        for endpoint in (item.source_entity_id, item.target_entity_id)
    }
    lock_entity_ids = sorted(neighbor_ids | entity_ids, key=str)
    if len(lock_entity_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict(
            "deletion_too_large",
            "Entity deletion graph closure exceeds 100",
            entity_ids=sorted(neighbor_ids | entity_ids, key=str),
            relationship_ids=[item.id for item in relationship_refs],
        )
    try:
        timeline_event_ids = await timeline.correction_event_ids(
            session, lock_entity_ids, scope=scope
        )
    except ValueError as exc:
        raise _conflict("deletion_too_large", str(exc), entity_ids=lock_entity_ids) from exc
    lock_entity_rows = list(
        (
            await session.scalars(
                select(Entity)
                .where(
                    Entity.workspace_id == scope.workspace_id,
                    Entity.id.in_(lock_entity_ids),
                )
                .order_by(Entity.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(lock_entity_rows) != len(lock_entity_ids):
        raise _conflict(
            "deletion_closure_changed",
            "An incident graph entity disappeared during deletion discovery",
            entity_ids=lock_entity_ids,
            relationship_ids=[item.id for item in relationship_refs],
        )
    pairs = sorted(
        {(item.document_version_id, item.chunk_id) for item in memberships}
        | {
            (support.document_version_id, support.chunk_id)
            for item in relationship_refs
            for support in item.supports
        },
        key=lambda item: (str(item[0]), str(item[1])),
    )
    membership_ids = (
        {item.id for item in memberships}
        | {
            identifier
            for item in relationship_refs
            for support in item.supports
            for identifier in (support.source_membership_id, support.target_membership_id)
            if identifier is not None
        }
        | {item.membership_id for item in alias_supports}
        | {item.membership_id for item in field_supports}
    )
    if (
        len(membership_ids) > MAX_CORRECTION_MEMBERSHIPS
        or len(pairs) > MAX_CORRECTION_EVIDENCE_REFS
    ):
        raise _conflict(
            "deletion_too_large",
            "Entity deletion evidence closure exceeds its atomic limit",
            entity_ids=sorted(entity_ids, key=str),
            membership_ids=sorted(membership_ids, key=str),
            relationship_ids=[item.id for item in relationship_refs],
        )
    source_ids = sorted(
        {item.source_id for item in memberships}
        | {
            support.source_id
            for item in relationship_refs
            for support in item.supports
            if support.source_id is not None
        }
        | {item.source_id for item in aliases if item.source_id is not None},
        key=str,
    )
    document_ids = sorted(
        {item.document_id for item in memberships}
        | {
            support.document_id
            for item in relationship_refs
            for support in item.supports
            if support.document_id is not None
        },
        key=str,
    )
    if len(document_ids) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict(
            "deletion_too_large",
            "Entity deletion document lock closure exceeds 100",
            entity_ids=sorted(entity_ids, key=str),
        )
    return _DeleteClosure(
        entity_rows,
        list(redirect_rows.values()),
        memberships,
        aliases,
        alias_supports,
        field_supports,
        decisions,
        relationship_refs,
        pairs,
        source_ids,
        document_ids,
        lock_entity_ids,
        timeline_event_ids,
    )


async def _lock_delete_closure(
    session: AsyncSession,
    before: _DeleteClosure,
    fence: AccessFence,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[UUID], list[UUID]]:
    """Take the deletion locks in the fixed order and return the locked relationship and support ids.

    Order: Sources (against ``fence``) -> retained Documents -> entities (sorted) -> redirect stubs ->
    relationship closure -> timeline events. A relationship closure too large to lock raises
    ``deletion_too_large``. Runs inside the caller's transaction; nothing is committed.
    """
    for source_id in before.source_ids:
        await sources.lock_source(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=fence,
        )
    if before.document_ids:
        # Documents deleted before this correction are already terminal; lock
        # every retained document that still exists without requiring historical
        # chunks/sources to remain readable.
        await documents.lock_document_ids(
            session,
            before.document_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    await entities.lock_entity_ids(
        session,
        before.lock_entity_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    redirect_ids = sorted({item.old_entity_id for item in before.redirect_rows}, key=str)
    if redirect_ids:
        await session.scalars(
            select(EntityRedirect.old_entity_id)
            .where(
                EntityRedirect.workspace_id == scope.workspace_id,
                EntityRedirect.old_entity_id.in_(redirect_ids),
            )
            .order_by(EntityRedirect.old_entity_id)
            .with_for_update()
        )
    try:
        relationship_ids, support_ids = await relationships.lock_delete_closure(
            session,
            before.entity_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "deletion_too_large",
            str(exc),
            entity_ids=before.entity_ids,
            relationship_ids=before.relationship_ids,
        ) from exc
    await timeline.lock_event_ids(
        session,
        before.timeline_event_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return relationship_ids, support_ids


async def _delete_closure_rows(
    session: AsyncSession,
    after: _DeleteClosure,
    entity_id: UUID,
    member_ids: list[UUID],
) -> dict[str, int | None]:
    """Delete the owned support rows, clear redirect stubs, blank non-root stubs and delete the root.

    Statement order matters for foreign keys: alias evidence -> field evidence -> decisions -> memberships ->
    aliases, then redirect targets are cleared (terminal stubs), stub entities are blanked with a revision
    bump, and the root row is deleted. Returns the pre-change revision of every closure entity for the audit.
    """
    await session.execute(
        delete(EntityAliasEvidence).where(
            EntityAliasEvidence.alias_id.in_([item.id for item in after.aliases])
        )
    )
    await session.execute(
        delete(EntityFieldEvidence).where(EntityFieldEvidence.entity_id.in_(after.entity_ids))
    )
    await session.execute(
        delete(EntityCorrectionDecision).where(
            or_(
                EntityCorrectionDecision.entity_id.in_(after.entity_ids),
                EntityCorrectionDecision.membership_id.in_(member_ids),
            )
        )
    )
    await session.execute(
        delete(EntityEvidenceMembership).where(
            EntityEvidenceMembership.entity_id.in_(after.entity_ids)
        )
    )
    await session.execute(delete(EntityAlias).where(EntityAlias.entity_id.in_(after.entity_ids)))
    for redirect in after.redirect_rows:
        redirect.target_entity_id = None
    for stub in after.entity_rows:
        if stub.id == entity_id:
            continue
        stub.name = None
        stub.canonical_name = None
        stub.description = None
        stub.name_origin = None
        stub.description_origin = None
        stub.metadata_json = {}
        stub.revision += 1
    root = next(item for item in after.entity_rows if item.id == entity_id)
    previous_revisions: dict[str, int | None] = {
        str(item.id): item.revision for item in after.entity_rows
    }
    await session.delete(root)
    return previous_revisions


async def delete_canonical_entity(
    session: AsyncSession,
    entity_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    reason: str,
) -> bool:
    """Delete a canonical root and its bounded support closure with an audit record.

    The admitted workspace scope supplies actor provenance for the audit.
    The function locks and revalidates the closure, removes owned support, and
    retains cleared redirect stubs with ``target_entity_id=None`` so deleted IDs
    stay terminal. It commits audit/realtime changes; oversized, changing, or
    noncanonical closures raise ``CorrectionConflictError``.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    before = await _discover_delete_closure(session, entity_id, scope=scope)
    before_signature = before.signature()
    relationship_ids, support_ids = await _lock_delete_closure(
        session,
        before,
        fence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    after = await _discover_delete_closure(session, entity_id, scope=scope)
    if before_signature != after.signature() or relationship_ids != after.relationship_ids:
        raise _conflict(
            "deletion_closure_changed",
            "Incident relationships changed while locks were acquired; retry",
            entity_ids=after.entity_ids,
            relationship_ids=relationship_ids,
        )
    member_ids = sorted(
        {item.id for item in after.memberships}
        | {
            identifier
            for ref in after.relationship_refs
            for support in ref.supports
            for identifier in (support.source_membership_id, support.target_membership_id)
            if identifier is not None
        }
        | {item.membership_id for item in after.alias_supports}
        | {item.membership_id for item in after.field_supports},
        key=str,
    )
    if member_ids:
        await entities.get_membership_refs(
            session,
            member_ids,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    final = await _discover_delete_closure(session, entity_id, scope=scope)
    if before_signature != final.signature():
        raise _conflict(
            "deletion_closure_changed",
            "Entity deletion support closure changed while membership locks were acquired; retry",
            entity_ids=final.entity_ids,
            relationship_ids=final.relationship_ids,
        )
    after = final
    await _temporal_before_relationships(
        session, relationship_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await _temporal_correction(
        session,
        after.memberships,
        [(row.id, row.revision) for row in after.entity_rows],
        "deleted",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        deleted=True,
    )
    try:
        await relationships.remove_entity_closure(
            session,
            after.entity_ids,
            relationship_ids,
            support_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "deletion_closure_changed",
            str(exc),
            entity_ids=after.entity_ids,
            relationship_ids=relationship_ids,
        ) from exc
    changed_timeline_ids = await timeline.remove_entity_participants(
        session,
        after.entity_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    previous_revisions = await _delete_closure_rows(session, after, entity_id, member_ids)
    affected_ids = sorted(
        {
            *after.entity_ids,
            *relationship_ids,
            *support_ids,
            *(item.id for item in after.memberships),
            *(item.id for item in after.aliases),
            *(item.id for item in after.alias_supports),
            *(item.id for item in after.field_supports),
            *(item.id for item in after.decisions),
        },
        key=str,
    )
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_delete",
        reason=reason,
        affected_ids=affected_ids,
        revisions=previous_revisions,
    )
    await session.flush()
    drafts: list[ReplayDraft] = [
        make_graph_change(entity_id=identifier, deleted=True, scope=scope)
        for identifier in after.entity_ids
    ]
    drafts.extend(
        make_graph_change(relationship_id=identifier, deleted=True, scope=scope)
        for identifier in relationship_ids
    )
    drafts.extend(
        await timeline.revise_corrected_events(
            session,
            changed_timeline_ids,
            entity_id=entity_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return True
