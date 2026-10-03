from __future__ import annotations

import json
from hashlib import sha256
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_graph_change
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities.models import (
    Entity, EntityAlias, EntityAliasEvidence, EntityCorrectionDecision,
    EntityEvidenceMembership, EntityFieldEvidence, EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    EntityCorrectionConflict, EntityCorrectionPreview, EntityCorrectionResult,
    EntityCreate, EntityMergeRequest, EntitySplitRequest, canonicalize_name,
    EntitySuppressionRequest,
)
from modules.knowledge.relationships import public as relationships
from modules.sources import public as sources
from modules.timeline import public as timeline

MAX_CORRECTION_ENTITIES = 100
MAX_CORRECTION_MEMBERSHIPS = 200
MAX_CORRECTION_EVIDENCE_REFS = 100


class CorrectionConflictError(ValueError):
    """Carry an API-ready correction conflict alongside its validation error."""

    def __init__(self, code: str, message: str, *, entity_ids: list[UUID] = [],
                 membership_ids: list[UUID] = [], relationship_ids: list[UUID] = []) -> None:
        """Build a conflict DTO containing the involved entity and support IDs."""
        super().__init__(message)
        self.conflict = EntityCorrectionConflict(
            code=code, message=message, entity_ids=entity_ids,
            membership_ids=membership_ids, relationship_ids=relationship_ids,
        )


@dataclass
class _Closure:
    """Hold the bounded entity, evidence, and relationship snapshot for correction."""
    entity_rows: list[Entity]
    memberships: list[EntityEvidenceMembership]
    aliases: list[EntityAlias]
    alias_supports: list[EntityAliasEvidence]
    field_supports: list[EntityFieldEvidence]
    relationship_refs: list[relationships.CorrectionRelationshipRef]
    redirect_rows: list[EntityRedirect]
    relationship_entity_ids: list[UUID]
    entity_ids: list[UUID]
    evidence_pairs: list[tuple[UUID, UUID]]
    source_ids: list[UUID]
    document_ids: list[UUID]
    membership_ids: list[UUID]
    timeline_event_ids: list[UUID]

    @property
    def relationship_ids(self) -> list[UUID]:
        """Return relationship IDs in the captured correction closure."""
        return [item.id for item in self.relationship_refs]

    def signature(self) -> str:
        """Serialize every relevant row field for optimistic closure revalidation."""
        value = {
            "entities": [(str(row.id), row.revision, row.type, row.name, row.canonical_name, row.description, row.name_origin, row.description_origin, row.metadata_json) for row in self.entity_rows],
            "memberships": [(str(row.id), str(row.entity_id), str(row.document_id), str(row.source_id), str(row.document_version_id), str(row.chunk_id), row.extraction_identity, row.candidate_key, row.match_fingerprint, row.observed_at.isoformat(), row.extracted_at.isoformat(), row.confidence) for row in self.memberships],
            "aliases": [(str(row.id), str(row.entity_id), row.normalized_alias, row.alias, str(row.source_id), row.confirmed, row.origin) for row in self.aliases],
            "alias_supports": [(str(row.id), str(row.alias_id), str(row.membership_id), row.confidence) for row in self.alias_supports],
            "field_supports": [(str(row.id), str(row.entity_id), row.field_name, row.value_hash, str(row.membership_id)) for row in self.field_supports],
            "relationships": [item.model_dump(mode="json") for item in self.relationship_refs],
            "redirects": [(str(row.old_entity_id), str(row.target_entity_id), row.actor_id, row.reason) for row in self.redirect_rows],
            "relationship_entities": [str(item) for item in self.relationship_entity_ids],
            "entity_ids": [str(item) for item in self.entity_ids],
            "evidence_pairs": [(str(left), str(right)) for left, right in self.evidence_pairs],
            "source_ids": [str(item) for item in self.source_ids],
            "document_ids": [str(item) for item in self.document_ids],
            "membership_ids": [str(item) for item in self.membership_ids],
            "timeline_events": [str(item) for item in self.timeline_event_ids],
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class _DeleteClosure:
    """Hold the bounded entity and graph rows discovered for canonical deletion."""
    entity_rows: list[Entity]
    redirect_rows: list[EntityRedirect]
    memberships: list[EntityEvidenceMembership]
    aliases: list[EntityAlias]
    alias_supports: list[EntityAliasEvidence]
    field_supports: list[EntityFieldEvidence]
    decisions: list[EntityCorrectionDecision]
    relationship_refs: list[relationships.CorrectionRelationshipRef]
    evidence_pairs: list[tuple[UUID, UUID]]
    source_ids: list[UUID]
    document_ids: list[UUID]
    lock_entity_ids: list[UUID]
    timeline_event_ids: list[UUID]

    @property
    def entity_ids(self) -> list[UUID]:
        """Return the sorted IDs of canonical and redirected entities in the closure."""
        return sorted((item.id for item in self.entity_rows), key=str)

    @property
    def relationship_ids(self) -> list[UUID]:
        """Return sorted relationship IDs captured for deletion."""
        return sorted((item.id for item in self.relationship_refs), key=str)

    def signature(self) -> str:
        """Serialize ownership, provenance, support, and graph state for revalidation."""
        return json.dumps({
        "entities": [(str(item.id), item.revision, item.name, item.canonical_name, item.description, item.name_origin, item.description_origin, item.metadata_json) for item in self.entity_rows],
            "redirects": [(str(item.old_entity_id), str(item.target_entity_id), str(item.actor_id), item.reason) for item in self.redirect_rows],
            "memberships": [(str(item.id), str(item.entity_id), str(item.document_id), str(item.source_id), str(item.document_version_id), str(item.chunk_id), item.extraction_identity, item.candidate_key, item.match_fingerprint, item.observed_at.isoformat(), item.extracted_at.isoformat(), item.confidence) for item in self.memberships],
            "aliases": [(str(item.id), str(item.entity_id), str(item.source_id), item.alias, item.normalized_alias, item.confirmed, item.origin, item.confidence) for item in self.aliases],
            "alias_supports": [(str(item.id), str(item.alias_id), str(item.membership_id), item.confidence) for item in self.alias_supports],
            "field_supports": [(str(item.id), str(item.entity_id), item.field_name, item.value_hash, str(item.membership_id)) for item in self.field_supports],
            "decisions": [(str(item.id), item.decision, item.scope, str(item.entity_id), str(item.document_id), str(item.membership_id), item.match_fingerprint, item.actor_id, item.reason, item.created_at.isoformat()) for item in self.decisions],
            "relationships": [item.model_dump(mode="json") for item in self.relationship_refs],
            "evidence": [(str(left), str(right)) for left, right in self.evidence_pairs],
            "sources": [str(item) for item in self.source_ids],
            "documents": [str(item) for item in self.document_ids],
            "lock_entities": [str(item) for item in self.lock_entity_ids],
            "timeline_events": [str(item) for item in self.timeline_event_ids],
        }, sort_keys=True, separators=(",", ":"))


def _conflict(code: str, message: str, *, entity_ids: list[UUID] = [],
              membership_ids: list[UUID] = [], relationship_ids: list[UUID] = []) -> CorrectionConflictError:
    """Build a structured correction conflict with the supplied affected IDs."""
    return CorrectionConflictError(code, message, entity_ids=entity_ids,
                                   membership_ids=membership_ids, relationship_ids=relationship_ids)


async def _discover(
    session: AsyncSession, entity_ids: list[UUID], *, include_target_memberships: bool = False
) -> _Closure:
    """Read and bound the merge/split support graph before acquiring its locks."""
    ids = sorted(set(entity_ids), key=str)
    entity_rows = list((await session.scalars(
        select(Entity).where(Entity.id.in_(ids)).order_by(Entity.id).execution_options(populate_existing=True)
    )).all()) if ids else []
    if len(entity_rows) != len(ids):
        raise _conflict("entity_missing", "A correction entity no longer exists", entity_ids=ids)
    source_memberships = list((await session.scalars(
        select(EntityEvidenceMembership).where(EntityEvidenceMembership.entity_id == ids[0]).order_by(EntityEvidenceMembership.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True)
    )).all()) if ids else []
    if include_target_memberships and len(ids) > 1:
        target_memberships = list((await session.scalars(
            select(EntityEvidenceMembership).where(EntityEvidenceMembership.entity_id == ids[1]).order_by(EntityEvidenceMembership.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True)
        )).all())
        source_memberships.extend(target_memberships)
    if len(source_memberships) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("correction_too_large", "Correction membership closure exceeds 200", entity_ids=ids)
    aliases = list((await session.scalars(
        select(EntityAlias).where(EntityAlias.entity_id.in_(ids)).order_by(EntityAlias.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True)
    )).all()) if ids else []
    if len(aliases) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("correction_too_large", "Correction alias closure exceeds 200", entity_ids=ids)
    alias_supports = list((await session.scalars(
        select(EntityAliasEvidence).where(EntityAliasEvidence.alias_id.in_([item.id for item in aliases])).order_by(EntityAliasEvidence.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True)
    )).all()) if aliases else []
    field_supports = list((await session.scalars(
        select(EntityFieldEvidence).where(EntityFieldEvidence.entity_id.in_(ids)).order_by(EntityFieldEvidence.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True)
    )).all()) if ids else []
    if len(alias_supports) > MAX_CORRECTION_MEMBERSHIPS or len(field_supports) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("correction_too_large", "Correction derived support closure exceeds 200", entity_ids=ids)
    owner_membership_ids = {item.id for item in source_memberships}
    support_membership_ids = {
        item.membership_id for item in alias_supports
    } | {item.membership_id for item in field_supports}
    if not support_membership_ids <= owner_membership_ids:
        raise _conflict("correction_closure_changed", "Derived support references evidence outside the correction membership closure", entity_ids=ids)
    redirect_rows_by_old_id: dict[UUID, EntityRedirect] = {}
    redirect_frontier = set(ids)
    while redirect_frontier:
        children = list((await session.scalars(select(EntityRedirect).where(
            EntityRedirect.target_entity_id.in_(redirect_frontier)
        ).order_by(EntityRedirect.old_entity_id).limit(MAX_CORRECTION_ENTITIES + 1).execution_options(populate_existing=True))).all())
        if len(children) > MAX_CORRECTION_ENTITIES:
            raise _conflict("correction_too_large", "Correction redirect closure exceeds 100", entity_ids=ids)
        next_frontier: set[UUID] = set()
        for redirect in children:
            if redirect.old_entity_id not in redirect_rows_by_old_id:
                redirect_rows_by_old_id[redirect.old_entity_id] = redirect
                if redirect.old_entity_id not in ids:
                    next_frontier.add(redirect.old_entity_id)
        if len(redirect_rows_by_old_id) > MAX_CORRECTION_ENTITIES:
            raise _conflict("correction_too_large", "Correction redirect closure exceeds 100", entity_ids=ids)
        if set(ids) & set(redirect_rows_by_old_id):
            raise _conflict("redirect_cycle", "Correction redirect closure contains a cycle", entity_ids=ids)
        redirect_frontier = next_frontier
    redirect_rows = sorted(redirect_rows_by_old_id.values(), key=lambda row: str(row.old_entity_id))
    redirect_entity_ids = set(redirect_rows_by_old_id)
    if redirect_entity_ids:
        retained_memberships = list((await session.scalars(select(EntityEvidenceMembership.id).where(
            EntityEvidenceMembership.entity_id.in_(redirect_entity_ids)
        ).order_by(EntityEvidenceMembership.id).limit(1))).all())
        retained_aliases = list((await session.scalars(select(EntityAlias.id).where(
            EntityAlias.entity_id.in_(redirect_entity_ids)
        ).order_by(EntityAlias.id).limit(1))).all())
        retained_fields = list((await session.scalars(select(EntityFieldEvidence.id).where(
            EntityFieldEvidence.entity_id.in_(redirect_entity_ids)
        ).order_by(EntityFieldEvidence.id).limit(1))).all())
        if retained_memberships or retained_aliases or retained_fields:
            raise _conflict("redirect_dependency_conflict", "Redirect closure contains retained entity-owned evidence that needs reconciliation", entity_ids=sorted(redirect_entity_ids, key=str), membership_ids=retained_memberships)
    entity_closure_ids = sorted(set(ids) | redirect_entity_ids, key=str)
    if len(entity_closure_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict("correction_too_large", "Correction redirect closure exceeds 100 entities", entity_ids=entity_closure_ids)
    try:
        relationship_refs = await relationships.list_correction_relationship_refs(session, entity_closure_ids)
    except ValueError as exc:
        raise _conflict("correction_too_large", str(exc), entity_ids=entity_closure_ids) from exc
    closure_entity_ids = sorted(set(entity_closure_ids) | {
        endpoint for item in relationship_refs for endpoint in (item.source_entity_id, item.target_entity_id)
    }, key=str)
    if len(closure_entity_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict("correction_too_large", "Correction neighbor closure exceeds 100 entities", entity_ids=closure_entity_ids)
    if closure_entity_ids != ids:
        entity_rows = list((await session.scalars(
            select(Entity).where(Entity.id.in_(closure_entity_ids)).order_by(Entity.id).execution_options(populate_existing=True)
        )).all())
        if len(entity_rows) != len(closure_entity_ids):
            raise _conflict("entity_missing", "A correction neighbor no longer exists", entity_ids=closure_entity_ids)
    pairs = sorted({
        (row.document_version_id, row.chunk_id) for row in source_memberships
    } | {
        (support.document_version_id, support.chunk_id)
        for ref in relationship_refs for support in ref.supports
    }, key=lambda pair: (str(pair[0]), str(pair[1])))
    relationship_membership_ids = {
        membership_id
        for ref in relationship_refs for support in ref.supports
        for membership_id in (support.source_membership_id, support.target_membership_id)
        if membership_id is not None
    }
    all_membership_ids = owner_membership_ids | support_membership_ids | relationship_membership_ids
    if len(pairs) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict("correction_too_large", "Correction evidence closure exceeds 100 references", entity_ids=closure_entity_ids)
    source_ids = sorted({row.source_id for row in source_memberships} | {
        support.source_id for ref in relationship_refs for support in ref.supports if support.source_id is not None
    } | {row.source_id for row in aliases if row.source_id is not None}, key=str)
    document_ids = sorted({row.document_id for row in source_memberships} | {
        support.document_id for ref in relationship_refs for support in ref.supports if support.document_id is not None
    }, key=str)
    if len(document_ids) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict("correction_too_large", "Correction document lock closure exceeds 100", entity_ids=closure_entity_ids)
    try:
        timeline_event_ids = await timeline.correction_event_ids(session, closure_entity_ids)
    except ValueError as exc:
        raise _conflict("correction_too_large", str(exc), entity_ids=closure_entity_ids) from exc
    return _Closure(
        entity_rows, source_memberships, aliases, alias_supports, field_supports,
        relationship_refs, redirect_rows, entity_closure_ids, closure_entity_ids, pairs, source_ids, document_ids,
        sorted(all_membership_ids, key=str), timeline_event_ids,
    )


async def _validate_merge_request(
    source_id: UUID, payload: EntityMergeRequest, closure: _Closure,
) -> tuple[Entity, Entity, list[EntityEvidenceMembership], dict[str, EntityAlias], list[EntityAlias]]:
    """Check revisions, types, protected fields, aliases, scope, and edge merge rules."""
    target_id = payload.into_id
    source = next((item for item in closure.entity_rows if item.id == source_id), None)
    target = next((item for item in closure.entity_rows if item.id == target_id), None)
    if source is None or target is None:
        raise _conflict("entity_missing", "Merge entity no longer exists", entity_ids=[source_id, target_id])
    if source.revision != payload.expected_revision or target.revision != payload.expected_into_revision:
        raise _conflict("stale_revision", "An entity changed; reload before merging", entity_ids=[source_id, target_id])
    if source.type != target.type:
        raise _conflict("incompatible_entity_types", "Entities of different types cannot be merged", entity_ids=[source_id, target_id])
    memberships = [item for item in closure.memberships if item.entity_id == source_id]
    await _check_future_scope(payload.future_document_id, memberships)
    target_aliases = {item.normalized_alias: item for item in closure.aliases if item.entity_id == target_id}
    source_aliases = [item for item in closure.aliases if item.entity_id == source_id]
    for alias in source_aliases:
        existing = target_aliases.get(alias.normalized_alias)
        if existing is not None and (existing.alias, existing.source_id, existing.confirmed, existing.origin) != (alias.alias, alias.source_id, alias.confirmed, alias.origin):
            raise _conflict("protected_alias_conflict", "Merge has conflicting aliases; resolve them before merging", entity_ids=[source_id, target_id])
        if existing is None and alias.origin == "owner":
            raise _conflict("protected_alias_conflict", "Merge cannot transfer owner-authored aliases across entity identities", entity_ids=[source_id, target_id])
    for field_name in ("name", "description"):
        source_value = getattr(source, field_name)
        source_origin = getattr(source, f"{field_name}_origin")
        target_value = getattr(target, field_name)
        target_origin = getattr(target, f"{field_name}_origin")
        if source_origin == "owner" and target_origin == "owner" and source_value != target_value:
            raise _conflict("protected_field_conflict", f"Merge has conflicting owner-authored {field_name}", entity_ids=[source_id, target_id])
        if field_name == "description" and source_value and target_value and source_value != target_value:
            raise _conflict("derived_field_conflict", "Merge has two different descriptions; resolve the field before merging", entity_ids=[source_id, target_id])
    try:
        redirect_ids = {row.old_entity_id for row in closure.redirect_rows}
        relationships.validate_entity_merge_plan(source_id, target_id, closure.relationship_refs, redirect_ids)
    except ValueError as exc:
        raise _conflict("relationship_merge_conflict", str(exc), entity_ids=[source_id, target_id], relationship_ids=closure.relationship_ids) from exc
    return source, target, memberships, target_aliases, source_aliases


async def _validate_split_request(
    entity_id: UUID, payload: EntitySplitRequest, closure: _Closure,
) -> tuple[Entity, list[EntityEvidenceMembership]]:
    """Check revision, replacement identity, selected memberships, and edge split rules."""
    source = next((item for item in closure.entity_rows if item.id == entity_id), None)
    if source is None:
        raise _conflict("entity_missing", "Split entity no longer exists", entity_ids=[entity_id])
    if source.revision != payload.expected_revision:
        raise _conflict("stale_revision", "Entity changed; reload before splitting", entity_ids=[entity_id])
    if payload.new_entity.type != source.type:
        raise _conflict("incompatible_entity_types", "Split entity must keep the original entity type", entity_ids=[entity_id])
    if canonicalize_name(payload.new_entity.name) == source.canonical_name:
        raise _conflict("split_identity_conflict", "Split entity must have a distinct canonical name", entity_ids=[entity_id])
    by_id = {item.id: item for item in closure.memberships}
    selected_ids = set(payload.evidence_ids)
    if not selected_ids or not selected_ids <= set(by_id) or any(by_id[item].entity_id != entity_id for item in selected_ids):
        raise _conflict("membership_unavailable", "Selected evidence is missing, stale, or belongs to another entity", entity_ids=[entity_id], membership_ids=payload.evidence_ids)
    selected = [by_id[item] for item in sorted(selected_ids, key=str)]
    await _check_future_scope(payload.future_document_id, selected)
    try:
        source_relationship_refs = [
            ref for ref in closure.relationship_refs
            if entity_id in (ref.source_entity_id, ref.target_entity_id)
        ]
        relationships.validate_entity_split_plan(entity_id, selected_ids, source_relationship_refs)
    except ValueError as exc:
        raise _conflict("relationship_split_conflict", str(exc), entity_ids=[entity_id], relationship_ids=closure.relationship_ids) from exc
    return source, selected


async def _discover_delete_closure(session: AsyncSession, entity_id: UUID) -> _DeleteClosure:
    """Discover bounded redirect, evidence, alias, decision, and incident-edge rows."""
    root = await session.scalar(select(Entity).where(Entity.id == entity_id).execution_options(populate_existing=True))
    if root is None:
        raise _conflict("entity_missing", "Entity no longer exists", entity_ids=[entity_id])
    entity_ids = {entity_id}
    frontier = {entity_id}
    redirect_rows: dict[UUID, EntityRedirect] = {}
    while frontier:
        children = list((await session.scalars(
            select(EntityRedirect).where(EntityRedirect.target_entity_id.in_(frontier))
            .order_by(EntityRedirect.old_entity_id).limit(MAX_CORRECTION_ENTITIES + 1).execution_options(populate_existing=True)
        )).all())
        if len(children) > MAX_CORRECTION_ENTITIES:
            raise _conflict("deletion_too_large", "Entity redirect closure exceeds 100", entity_ids=sorted(entity_ids, key=str))
        next_frontier: set[UUID] = set()
        for redirect in children:
            if redirect.old_entity_id not in entity_ids:
                entity_ids.add(redirect.old_entity_id)
                next_frontier.add(redirect.old_entity_id)
            redirect_rows[redirect.old_entity_id] = redirect
        if len(entity_ids) > MAX_CORRECTION_ENTITIES:
            raise _conflict("deletion_too_large", "Entity redirect closure exceeds 100", entity_ids=sorted(entity_ids, key=str))
        frontier = next_frontier
    redirect_rows.update({item.old_entity_id: item for item in (await session.scalars(
        select(EntityRedirect).where(EntityRedirect.old_entity_id.in_(entity_ids)).order_by(EntityRedirect.old_entity_id).execution_options(populate_existing=True)
    )).all()})
    if entity_id in redirect_rows:
        if redirect_rows[entity_id].target_entity_id is None:
            raise _conflict("entity_missing", "Entity was deleted", entity_ids=[entity_id])
        raise _conflict("entity_redirected", "Only canonical entities can be deleted", entity_ids=[entity_id])
    entity_rows = list((await session.scalars(select(Entity).where(Entity.id.in_(entity_ids)).order_by(Entity.id).execution_options(populate_existing=True))).all())
    if len(entity_rows) != len(entity_ids):
        raise _conflict("deletion_closure_changed", "Redirect entity disappeared during deletion discovery", entity_ids=sorted(entity_ids, key=str))
    memberships = list((await session.scalars(select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.entity_id.in_(entity_ids)
    ).order_by(EntityEvidenceMembership.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True))).all())
    aliases = list((await session.scalars(select(EntityAlias).where(
        EntityAlias.entity_id.in_(entity_ids)
    ).order_by(EntityAlias.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True))).all())
    decisions = list((await session.scalars(select(EntityCorrectionDecision).where(
        or_(EntityCorrectionDecision.entity_id.in_(entity_ids), EntityCorrectionDecision.membership_id.in_(
            select(EntityEvidenceMembership.id).where(EntityEvidenceMembership.entity_id.in_(entity_ids))
        )))
        .order_by(EntityCorrectionDecision.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True))).all())
    if len(memberships) > MAX_CORRECTION_MEMBERSHIPS or len(aliases) > MAX_CORRECTION_MEMBERSHIPS or len(decisions) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("deletion_too_large", "Entity deletion closure exceeds 200 owned rows", entity_ids=sorted(entity_ids, key=str))
    alias_supports = list((await session.scalars(select(EntityAliasEvidence).where(
        EntityAliasEvidence.alias_id.in_([item.id for item in aliases])
    ).order_by(EntityAliasEvidence.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True))).all()) if aliases else []
    field_supports = list((await session.scalars(select(EntityFieldEvidence).where(
        EntityFieldEvidence.entity_id.in_(entity_ids)
    ).order_by(EntityFieldEvidence.id).limit(MAX_CORRECTION_MEMBERSHIPS + 1).execution_options(populate_existing=True))).all())
    if len(alias_supports) > MAX_CORRECTION_MEMBERSHIPS or len(field_supports) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("deletion_too_large", "Entity deletion support closure exceeds 200", entity_ids=sorted(entity_ids, key=str))
    try:
        relationship_refs = await relationships.list_correction_relationship_refs(session, sorted(entity_ids, key=str))
    except ValueError as exc:
        raise _conflict("deletion_too_large", str(exc), entity_ids=sorted(entity_ids, key=str)) from exc
    neighbor_ids = {endpoint for item in relationship_refs for endpoint in (item.source_entity_id, item.target_entity_id)}
    lock_entity_ids = sorted(neighbor_ids | entity_ids, key=str)
    if len(lock_entity_ids) > MAX_CORRECTION_ENTITIES:
        raise _conflict("deletion_too_large", "Entity deletion graph closure exceeds 100", entity_ids=sorted(neighbor_ids | entity_ids, key=str), relationship_ids=[item.id for item in relationship_refs])
    try:
        timeline_event_ids = await timeline.correction_event_ids(session, lock_entity_ids)
    except ValueError as exc:
        raise _conflict("deletion_too_large", str(exc), entity_ids=lock_entity_ids) from exc
    lock_entity_rows = list((await session.scalars(select(Entity).where(Entity.id.in_(lock_entity_ids)).order_by(Entity.id).execution_options(populate_existing=True))).all())
    if len(lock_entity_rows) != len(lock_entity_ids):
        raise _conflict("deletion_closure_changed", "An incident graph entity disappeared during deletion discovery", entity_ids=lock_entity_ids, relationship_ids=[item.id for item in relationship_refs])
    pairs = sorted({(item.document_version_id, item.chunk_id) for item in memberships} | {
        (support.document_version_id, support.chunk_id) for item in relationship_refs for support in item.supports
    }, key=lambda item: (str(item[0]), str(item[1])))
    membership_ids = {item.id for item in memberships} | {
        identifier for item in relationship_refs for support in item.supports
        for identifier in (support.source_membership_id, support.target_membership_id) if identifier is not None
    } | {item.membership_id for item in alias_supports} | {item.membership_id for item in field_supports}
    if len(membership_ids) > MAX_CORRECTION_MEMBERSHIPS or len(pairs) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict("deletion_too_large", "Entity deletion evidence closure exceeds its atomic limit", entity_ids=sorted(entity_ids, key=str), membership_ids=sorted(membership_ids, key=str), relationship_ids=[item.id for item in relationship_refs])
    source_ids = sorted({item.source_id for item in memberships} | {
        support.source_id for item in relationship_refs for support in item.supports if support.source_id is not None
    } | {item.source_id for item in aliases if item.source_id is not None}, key=str)
    document_ids = sorted({item.document_id for item in memberships} | {
        support.document_id for item in relationship_refs for support in item.supports if support.document_id is not None
    }, key=str)
    if len(document_ids) > MAX_CORRECTION_EVIDENCE_REFS:
        raise _conflict("deletion_too_large", "Entity deletion document lock closure exceeds 100", entity_ids=sorted(entity_ids, key=str))
    return _DeleteClosure(entity_rows, list(redirect_rows.values()), memberships, aliases,
                          alias_supports, field_supports, decisions, relationship_refs,
                          pairs, source_ids, document_ids, lock_entity_ids, timeline_event_ids)


async def delete_canonical_entity(
    session: AsyncSession, entity_id: UUID, *, actor_id: int, reason: str,
) -> bool:
    """Delete a canonical root and its bounded support closure with an audit record.

    The owner-write route supplies authorization and ``actor_id`` for the audit.
    The function locks and revalidates the closure, removes owned support, and
    retains cleared redirect stubs with ``target_entity_id=None`` so deleted IDs
    stay terminal. It commits audit/realtime changes; oversized, changing, or
    noncanonical closures raise ``CorrectionConflictError``.
    """
    before = await _discover_delete_closure(session, entity_id)
    before_signature = before.signature()
    for source_id in before.source_ids:
        await sources.lock_source(session, source_id)
    if before.document_ids:
        # Documents deleted before this correction are already terminal; lock
        # every retained document that still exists without requiring historical
        # chunks/sources to remain readable.
        await documents.lock_document_ids(session, before.document_ids)
    await entities.lock_entity_ids(session, before.lock_entity_ids)
    redirect_ids = sorted({item.old_entity_id for item in before.redirect_rows}, key=str)
    if redirect_ids:
        await session.scalars(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.old_entity_id.in_(redirect_ids)
        ).order_by(EntityRedirect.old_entity_id).with_for_update())
    try:
        relationship_ids, support_ids = await relationships.lock_delete_closure(session, before.entity_ids)
    except ValueError as exc:
        raise _conflict("deletion_too_large", str(exc), entity_ids=before.entity_ids, relationship_ids=before.relationship_ids) from exc
    await timeline.lock_event_ids(session, before.timeline_event_ids)
    after = await _discover_delete_closure(session, entity_id)
    if before_signature != after.signature() or relationship_ids != after.relationship_ids:
        raise _conflict("deletion_closure_changed", "Incident relationships changed while locks were acquired; retry", entity_ids=after.entity_ids, relationship_ids=relationship_ids)
    member_ids = sorted({item.id for item in after.memberships} | {
        identifier for ref in after.relationship_refs for support in ref.supports
        for identifier in (support.source_membership_id, support.target_membership_id) if identifier is not None
    } | {item.membership_id for item in after.alias_supports} | {item.membership_id for item in after.field_supports}, key=str)
    if member_ids:
        await entities.get_membership_refs(session, member_ids, for_write=True)
    final = await _discover_delete_closure(session, entity_id)
    if before_signature != final.signature():
        raise _conflict(
            "deletion_closure_changed",
            "Entity deletion support closure changed while membership locks were acquired; retry",
            entity_ids=final.entity_ids,
            relationship_ids=final.relationship_ids,
        )
    after = final
    await _temporal_before_relationships(session, relationship_ids)
    await _temporal_correction(session, after.memberships,
                               [(row.id, row.revision) for row in after.entity_rows], "deleted", deleted=True)
    try:
        await relationships.remove_entity_closure(session, after.entity_ids, relationship_ids, support_ids)
    except ValueError as exc:
        raise _conflict("deletion_closure_changed", str(exc), entity_ids=after.entity_ids, relationship_ids=relationship_ids) from exc
    changed_timeline_ids = await timeline.remove_entity_participants(session, after.entity_ids)
    await session.execute(delete(EntityAliasEvidence).where(EntityAliasEvidence.alias_id.in_([item.id for item in after.aliases])))
    await session.execute(delete(EntityFieldEvidence).where(EntityFieldEvidence.entity_id.in_(after.entity_ids)))
    await session.execute(delete(EntityCorrectionDecision).where(or_(
        EntityCorrectionDecision.entity_id.in_(after.entity_ids),
        EntityCorrectionDecision.membership_id.in_(member_ids),
    )))
    await session.execute(delete(EntityEvidenceMembership).where(EntityEvidenceMembership.entity_id.in_(after.entity_ids)))
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
    previous_revisions = {str(item.id): item.revision for item in after.entity_rows}
    await session.delete(root)
    affected_ids = sorted({
        *after.entity_ids, *relationship_ids, *support_ids,
        *(item.id for item in after.memberships), *(item.id for item in after.aliases),
        *(item.id for item in after.alias_supports), *(item.id for item in after.field_supports),
        *(item.id for item in after.decisions),
    }, key=str)
    await entities.record_owner_action(
        session, actor_id=actor_id, operation="entity_delete", reason=reason,
        affected_ids=affected_ids, revisions=previous_revisions,
    )
    await session.flush()
    drafts = [make_graph_change(entity_id=identifier, deleted=True) for identifier in after.entity_ids]
    drafts.extend(make_graph_change(relationship_id=identifier, deleted=True) for identifier in relationship_ids)
    drafts.extend(await timeline.revise_corrected_events(session, changed_timeline_ids, entity_id=entity_id))
    await commit_with_replay(session, drafts)
    return True


async def _locked_closure(
    session: AsyncSession, entity_ids: list[UUID], *, include_target_memberships: bool = False
) -> _Closure:
    """Lock evidence owners and sorted graph rows, then reject any changed snapshot."""
    before = await _discover(session, entity_ids, include_target_memberships=include_target_memberships)
    before_signature = before.signature()
    try:
        evidence_refs = await documents.read_evidence_refs(session, before.evidence_pairs)
    except ValueError as exc:
        raise _conflict("evidence_unavailable", "Correction evidence is no longer retained", entity_ids=before.entity_ids) from exc
    source_ids = sorted(set(before.source_ids) | {item.source_id for item in evidence_refs}, key=str)
    for source_id in source_ids:
        if await sources.lock_source(session, source_id) is None:
            raise _conflict("source_unavailable", "Correction source dependency is no longer retained", entity_ids=before.entity_ids)
    document_ids = sorted(set(before.document_ids) | {item.document_id for item in evidence_refs}, key=str)
    if document_ids:
        await documents.lock_document_ids(session, document_ids)
    try:
        verified_refs = await documents.read_evidence_refs(session, before.evidence_pairs)
    except ValueError as exc:
        raise _conflict("evidence_unavailable", "Correction evidence changed before locking", entity_ids=before.entity_ids) from exc
    if verified_refs != evidence_refs:
        raise _conflict("evidence_unavailable", "Correction evidence changed before locking", entity_ids=before.entity_ids)
    await session.scalars(select(Entity.id).where(
        Entity.id.in_(before.entity_ids)
    ).order_by(Entity.id).with_for_update().execution_options(populate_existing=True))
    redirect_ids = sorted({item.old_entity_id for item in before.redirect_rows}, key=str)
    if redirect_ids:
        await session.scalars(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.old_entity_id.in_(redirect_ids)
        ).order_by(EntityRedirect.old_entity_id).with_for_update().execution_options(populate_existing=True))
    try:
        await relationships.lock_correction_closure(session, before.relationship_entity_ids, set(before.relationship_ids))
    except ValueError as exc:
        raise _conflict("correction_closure_changed", str(exc), entity_ids=before.entity_ids, relationship_ids=before.relationship_ids) from exc
    await timeline.lock_event_ids(session, before.timeline_event_ids)
    after = await _discover(session, entity_ids, include_target_memberships=include_target_memberships)
    if before_signature != after.signature():
        raise _conflict("correction_closure_changed", "Correction closure changed while locks were acquired; retry", entity_ids=before.entity_ids, membership_ids=[item.id for item in before.memberships], relationship_ids=before.relationship_ids)
    memberships = sorted({item.id for item in after.memberships} | {
        support.source_membership_id for ref in after.relationship_refs for support in ref.supports if support.source_membership_id is not None
    } | {
        support.target_membership_id for ref in after.relationship_refs for support in ref.supports if support.target_membership_id is not None
    } | {item.membership_id for item in after.alias_supports} | {item.membership_id for item in after.field_supports}, key=str)
    memberships = sorted(set(memberships) | set(after.membership_ids), key=str)
    if len(memberships) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict("correction_too_large", "Correction membership closure exceeds 200", entity_ids=after.entity_ids)
    if memberships:
        await entities.get_membership_refs(session, memberships, for_write=True)
    verified = await _discover(session, entity_ids, include_target_memberships=include_target_memberships)
    if before_signature != verified.signature():
        raise _conflict("correction_closure_changed", "Correction support closure changed while membership locks were acquired; retry", entity_ids=before.entity_ids, membership_ids=[item.id for item in before.memberships], relationship_ids=before.relationship_ids)
    after = verified
    return after


async def preview_merge(session: AsyncSession, source_id: UUID, payload: EntityMergeRequest) -> EntityCorrectionPreview:
    """Return merge impact and conflicts without writing or locking a correction closure."""
    try:
        if source_id == payload.into_id:
            raise _conflict("self_merge", "An entity cannot be merged into itself", entity_ids=[source_id])
        if await entities.resolve_canonical_entity_id(session, source_id) != source_id or await entities.resolve_canonical_entity_id(session, payload.into_id) != payload.into_id:
            raise _conflict("redirected_entity", "Use canonical entity IDs for corrections", entity_ids=[source_id, payload.into_id])
        closure = await _discover(session, [source_id, payload.into_id], include_target_memberships=True)
        await _validate_merge_request(source_id, payload, closure)
        return EntityCorrectionPreview(
            operation="merge", entity_ids=closure.entity_ids,
            membership_ids=[item.id for item in closure.memberships],
            relationship_ids=closure.relationship_ids, evidence_ref_count=len(closure.evidence_pairs),
        )
    except CorrectionConflictError as exc:
        return EntityCorrectionPreview(operation="merge", entity_ids=[source_id, payload.into_id], membership_ids=[], relationship_ids=[], evidence_ref_count=0, conflicts=[exc.conflict])
    except (LookupError, ValueError) as exc:
        conflict = EntityCorrectionConflict(
            code="entity_missing" if isinstance(exc, LookupError) else "entity_identity_unavailable",
            message="An entity is missing, deleted, or has an unavailable redirect chain",
            entity_ids=[source_id, payload.into_id], membership_ids=[], relationship_ids=[],
        )
        return EntityCorrectionPreview(operation="merge", entity_ids=[source_id, payload.into_id], membership_ids=[], relationship_ids=[], evidence_ref_count=0, conflicts=[conflict])


async def preview_split(session: AsyncSession, entity_id: UUID, payload: EntitySplitRequest) -> EntityCorrectionPreview:
    """Return split impact and conflicts without applying the requested correction."""
    try:
        if await entities.resolve_canonical_entity_id(session, entity_id) != entity_id:
            raise _conflict("redirected_entity", "Use the canonical entity ID for corrections", entity_ids=[entity_id])
        closure = await _discover(session, [entity_id])
        await _validate_split_request(entity_id, payload, closure)
        return EntityCorrectionPreview(
            operation="split", entity_ids=closure.entity_ids,
            membership_ids=payload.evidence_ids, relationship_ids=closure.relationship_ids,
            evidence_ref_count=len(closure.evidence_pairs),
        )
    except CorrectionConflictError as exc:
        return EntityCorrectionPreview(operation="split", entity_ids=[entity_id], membership_ids=payload.evidence_ids, relationship_ids=[], evidence_ref_count=0, conflicts=[exc.conflict])
    except (LookupError, ValueError) as exc:
        conflict = EntityCorrectionConflict(
            code="entity_missing" if isinstance(exc, LookupError) else "entity_identity_unavailable",
            message="Entity is missing, deleted, or has an unavailable redirect chain",
            entity_ids=[entity_id], membership_ids=[], relationship_ids=[],
        )
        return EntityCorrectionPreview(operation="split", entity_ids=[entity_id], membership_ids=payload.evidence_ids, relationship_ids=[], evidence_ref_count=0, conflicts=[conflict])


async def _check_future_scope(
    document_id: UUID | None, memberships: list[EntityEvidenceMembership],
) -> list[EntityEvidenceMembership]:
    """Select evidence in the future document only when each row has a fingerprint."""
    if document_id is None:
        return []
    scoped = [item for item in memberships if item.document_id == document_id]
    if not scoped or any(item.match_fingerprint is None for item in scoped):
        raise _conflict("future_scope_unavailable", "Future document scope requires selected evidence with a usable candidate fingerprint", membership_ids=[item.id for item in scoped])
    return scoped


async def _record_assignments(
    session: AsyncSession, memberships: list[EntityEvidenceMembership], target_id: UUID, *,
    actor_id: int, reason: str, future_document_id: UUID | None,
) -> None:
    """Upsert evidence- and optional document-scoped owner assignment decisions."""
    now = datetime.now(UTC)
    for membership in memberships:
        fingerprint = membership.match_fingerprint
        if fingerprint is None:
            continue
        old = await session.scalar(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "evidence",
            EntityCorrectionDecision.membership_id == membership.id,
        ).with_for_update())
        if old is None:
            session.add(EntityCorrectionDecision(
                decision="assign", scope="evidence", entity_id=target_id,
                membership_id=membership.id, match_fingerprint=fingerprint,
                actor_id=actor_id, reason=reason, created_at=now,
            ))
        else:
            old.decision, old.entity_id = "assign", target_id
            old.match_fingerprint, old.actor_id, old.reason, old.created_at = fingerprint, actor_id, reason, now
    scoped = await _check_future_scope(future_document_id, memberships)
    for fingerprint in sorted({item.match_fingerprint for item in scoped if item.match_fingerprint is not None}):
        prior = (await session.scalars(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "document",
            EntityCorrectionDecision.document_id == future_document_id,
            EntityCorrectionDecision.match_fingerprint == fingerprint,
        ).order_by(EntityCorrectionDecision.created_at, EntityCorrectionDecision.id).with_for_update())).all()
        if len({(item.decision, item.entity_id) for item in prior}) > 1:
            raise _conflict("conflicting_owner_corrections", "Future selector already has conflicting owner rules", membership_ids=[item.id for item in scoped])
        if prior:
            rule = prior[-1]
            rule.decision, rule.entity_id = "assign", target_id
            rule.actor_id, rule.reason, rule.created_at = actor_id, reason, now
        else:
            session.add(EntityCorrectionDecision(
                decision="assign", scope="document", entity_id=target_id,
                document_id=future_document_id, match_fingerprint=fingerprint,
                actor_id=actor_id, reason=reason, created_at=now,
            ))


async def merge_entity(
    session: AsyncSession, source_id: UUID, payload: EntityMergeRequest, *, actor_id: int
) -> EntityCorrectionResult:
    """Merge two canonical entities and commit their audited support migration.

    Authorization is enforced by the owner-write route; ``actor_id`` is audit
    provenance. The function locks/revalidates a bounded closure, redirects the
    source identity to the target, preserves evidence/document assignment rules,
    and rebinds only relationships whose support remains valid. Conflicts raise
    ``CorrectionConflictError`` and successful changes commit with graph events.
    """
    if source_id == payload.into_id:
        raise _conflict("self_merge", "An entity cannot be merged into itself", entity_ids=[source_id])
    if await entities.resolve_canonical_entity_id(session, source_id) != source_id or await entities.resolve_canonical_entity_id(session, payload.into_id) != payload.into_id:
        raise _conflict("redirected_entity", "Use canonical entity IDs for corrections", entity_ids=[source_id, payload.into_id])
    closure = await _locked_closure(session, [source_id, payload.into_id], include_target_memberships=True)
    await _temporal_before_relationships(session, closure.relationship_ids)
    source, target, memberships, target_aliases, source_aliases = await _validate_merge_request(source_id, payload, closure)
    previous_revisions = {str(source.id): source.revision, str(target.id): target.revision}
    for field_name in ("name", "description"):
        source_origin = getattr(source, f"{field_name}_origin")
        target_origin = getattr(target, f"{field_name}_origin")
        source_value = getattr(source, field_name)
        if source_origin == "owner" and target_origin != "owner":
            setattr(target, field_name, source_value)
            setattr(target, f"{field_name}_origin", "owner")
            if field_name == "name":
                target.canonical_name = canonicalize_name(source_value) if source_value else None
            await session.execute(delete(EntityFieldEvidence).where(
                EntityFieldEvidence.entity_id == target.id, EntityFieldEvidence.field_name == field_name,
            ))
        elif source_value and getattr(target, field_name) is None and source_origin == "derived":
            setattr(target, field_name, source_value)
            setattr(target, f"{field_name}_origin", "derived")
            if field_name == "name":
                target.canonical_name = canonicalize_name(source_value)
    source_derived_name = source.name if source.name_origin == "derived" else None
    if source_derived_name and target.name and canonicalize_name(source_derived_name) != target.canonical_name:
        normalized = canonicalize_name(source_derived_name)
        alias = target_aliases.get(normalized)
        if alias is None:
            alias = EntityAlias(
                entity_id=target.id, alias=source_derived_name, normalized_alias=normalized,
                confirmed=False, origin="derived", confidence=None,
            )
            session.add(alias)
            await session.flush()
            target_aliases[normalized] = alias
        membership_by_id = {item.id: item for item in memberships}
        name_hash = sha256(source_derived_name.encode("utf-8")).hexdigest()
        for support in closure.field_supports:
            membership = membership_by_id.get(support.membership_id)
            if support.entity_id == source.id and support.field_name == "name" and support.value_hash == name_hash and membership is not None:
                session.add(EntityAliasEvidence(alias_id=alias.id, membership_id=membership.id, confidence=membership.confidence))
    membership_by_id = {item.id: item for item in memberships}
    for alias in source_aliases:
        existing = target_aliases.get(alias.normalized_alias)
        if existing is None:
            for support in closure.alias_supports:
                if support.alias_id == alias.id and support.membership_id in membership_by_id:
                    support.alias_id = alias.id
            alias.entity_id = target.id
            continue
        supports = [item for item in closure.alias_supports if item.alias_id == alias.id]
        target_supports = {item.membership_id: item for item in closure.alias_supports if item.alias_id == existing.id}
        for support in supports:
            target_support = target_supports.get(support.membership_id)
            if target_support is None:
                support.alias_id = existing.id
            else:
                target_support.confidence = max(target_support.confidence, support.confidence)
                await session.delete(support)
        await session.delete(alias)
    await _record_assignments(session, memberships, target.id, actor_id=actor_id, reason=payload.reason, future_document_id=payload.future_document_id)
    for membership in memberships:
        membership.entity_id = target.id
    for support in closure.field_supports:
        if support.entity_id != source.id:
            continue
        value = target.name if support.field_name == "name" else target.description
        value_origin = target.name_origin if support.field_name == "name" else target.description_origin
        if value_origin == "derived" and value is not None and support.value_hash == sha256(value.encode("utf-8")).hexdigest():
            support.entity_id = target.id
        else:
            await session.delete(support)
    try:
        replacements = await relationships.apply_entity_merge(
            session, source.id, target.id, set(closure.relationship_ids),
            closure.relationship_entity_ids, {row.old_entity_id for row in closure.redirect_rows},
        )
    except ValueError as exc:
        raise _conflict("relationship_merge_conflict", str(exc), entity_ids=[source.id, target.id], relationship_ids=closure.relationship_ids) from exc
    changed_timeline_ids = await timeline.apply_entity_merge(
        session, source_id=source.id, target_id=target.id,
        event_ids=closure.timeline_event_ids,
    )
    for redirect in closure.redirect_rows:
        redirect.target_entity_id = target.id
    session.add(EntityRedirect(old_entity_id=source.id, target_entity_id=target.id, actor_id=actor_id, reason=" ".join(payload.reason.split()), created_at=datetime.now(UTC)))
    if source.name_origin == "derived":
        source.name = source.canonical_name = None
        source.name_origin = None
    if source.description_origin == "derived":
        source.description = None
        source.description_origin = None
    source.revision += 1
    target.revision += 1
    await entities.record_owner_action(
        session, actor_id=actor_id, operation="entity_merge", reason=payload.reason,
        affected_ids=[source.id, target.id, *[row.old_entity_id for row in closure.redirect_rows], *[old for old, _ in replacements]], revisions=previous_revisions,
    )
    await session.flush()
    result = EntityCorrectionResult(
        operation="merge", entity_id=source.id, canonical_entity_id=target.id,
        replacement_entity_ids=sorted({new for _, new in replacements}, key=str),
        revision=target.revision,
    )
    drafts = [make_graph_change(entity_id=target.id)]
    drafts.extend(make_graph_change(relationship_id=new) for _, new in replacements)
    drafts.extend(await timeline.revise_corrected_events(session, changed_timeline_ids, entity_id=target.id))
    await _temporal_correction(session, closure.memberships,
                               [(source.id, source.revision), (target.id, target.revision)], "merge")
    await commit_with_replay(session, drafts)
    return result


async def split_entity(
    session: AsyncSession, entity_id: UUID, payload: EntitySplitRequest, *, actor_id: int
) -> EntityCorrectionResult:
    """Move selected evidence into a new owner-authored entity and commit the audit.

    The owner-write route authorizes the operation and supplies ``actor_id`` for
    provenance. After locking and validating the bounded closure, it moves chosen
    memberships and supported relationships; new identity fields remain
    owner-authored rather than inheriting derived field support. Conflicts raise
    ``CorrectionConflictError``.
    """
    if await entities.resolve_canonical_entity_id(session, entity_id) != entity_id:
        raise _conflict("redirected_entity", "Use the canonical entity ID for corrections", entity_ids=[entity_id])
    closure = await _locked_closure(session, [entity_id])
    source, selected = await _validate_split_request(entity_id, payload, closure)
    await _temporal_before_relationships(session, closure.relationship_ids)
    selected_ids = set(payload.evidence_ids)
    new_id = uuid4()
    new_entity = Entity(
        id=new_id, type=payload.new_entity.type, name=payload.new_entity.name,
        canonical_name=canonicalize_name(payload.new_entity.name), description=payload.new_entity.description,
        name_origin="owner", description_origin="owner" if payload.new_entity.description is not None else None,
        metadata_json=payload.new_entity.metadata, revision=1,
    )
    session.add(new_entity)
    await session.flush()
    new_aliases = [EntityAlias(
        entity_id=new_id, alias=value, normalized_alias=canonicalize_name(value),
        confirmed=True, origin="owner",
    ) for value in payload.new_entity.aliases if canonicalize_name(value) != new_entity.canonical_name]
    session.add_all(new_aliases)
    await session.flush()
    previous_revision = source.revision
    await _record_assignments(session, selected, new_id, actor_id=actor_id, reason=payload.reason, future_document_id=payload.future_document_id)
    selected_supports = [item for item in closure.alias_supports if item.membership_id in selected_ids]
    for support in selected_supports:
        alias = next(item for item in closure.aliases if item.id == support.alias_id)
        if alias.origin == "owner":
            await session.delete(support)
            continue
        existing = next((item for item in new_aliases if item.normalized_alias == alias.normalized_alias), None)
        if existing is None:
            existing = EntityAlias(
                entity_id=new_id, alias=alias.alias, normalized_alias=alias.normalized_alias,
                source_id=alias.source_id, confirmed=alias.confirmed, origin="derived", confidence=alias.confidence,
            )
            session.add(existing)
            new_aliases.append(existing)
            await session.flush()
        support.alias_id = existing.id
    for item in selected:
        item.entity_id = new_id
    # New identity fields are owner-authored; selected derived support cannot override them.
    for support in closure.field_supports:
        if support.membership_id in selected_ids:
            await session.delete(support)
    for alias in closure.aliases:
        if alias.entity_id != entity_id or alias.origin != "derived":
            continue
        remaining = (await session.scalars(select(EntityAliasEvidence.id).where(EntityAliasEvidence.alias_id == alias.id))).all()
        if not remaining and any(item.alias_id == alias.id for item in selected_supports):
            await session.delete(alias)
    try:
        source_relationship_ids = {
            ref.id for ref in closure.relationship_refs
            if entity_id in (ref.source_entity_id, ref.target_entity_id)
        }
        replacements = await relationships.apply_entity_split(
            session, entity_id, new_id, selected_ids, source_relationship_ids
        )
    except ValueError as exc:
        raise _conflict("relationship_split_conflict", str(exc), entity_ids=[entity_id], relationship_ids=closure.relationship_ids) from exc
    changed_timeline_ids = await timeline.apply_entity_split(
        session, source_id=entity_id, target_id=new_id,
        event_ids=closure.timeline_event_ids,
        selected_pairs={(item.document_version_id, item.chunk_id) for item in selected},
    )
    for field_name in ("name", "description"):
        if getattr(source, f"{field_name}_origin") != "derived":
            continue
        remaining = await session.scalar(select(EntityFieldEvidence.id).where(
            EntityFieldEvidence.entity_id == entity_id, EntityFieldEvidence.field_name == field_name,
        ).limit(1))
        if remaining is None and any(item.entity_id == entity_id and item.field_name == field_name and item.membership_id in selected_ids for item in closure.field_supports):
            setattr(source, field_name, None)
            setattr(source, f"{field_name}_origin", None)
            if field_name == "name":
                source.canonical_name = None
    source.revision += 1
    await entities.record_owner_action(
        session, actor_id=actor_id, operation="entity_split", reason=payload.reason,
        affected_ids=[source.id, new_id, *selected_ids, *[old for old, _ in replacements]],
        revisions={str(source.id): previous_revision, str(new_id): 1},
    )
    await session.flush()
    result = EntityCorrectionResult(
        operation="split", entity_id=source.id, canonical_entity_id=source.id,
        replacement_entity_ids=[new_id], revision=source.revision,
    )
    drafts = [make_graph_change(entity_id=source.id), make_graph_change(entity_id=new_id)]
    drafts.extend(make_graph_change(relationship_id=new) for _, new in replacements)
    drafts.extend(await timeline.revise_corrected_events(session, changed_timeline_ids, entity_id=source.id))
    await _temporal_correction(session, closure.memberships,
                               [(source.id, source.revision), (new_entity.id, new_entity.revision)], "split")
    await commit_with_replay(session, drafts)
    return result


async def suppress_candidates(
    session: AsyncSession, entity_id: UUID, payload: EntitySuppressionRequest, *, actor_id: int
) -> EntityCorrectionResult:
    """Commit owner suppression rules for selected evidence and future documents.

    Authorization is enforced by the owner-write route; ``actor_id`` and reason
    are recorded in the audit. The operation requires a current canonical
    revision and records scoped decisions without deleting memberships/evidence
    or incrementing the entity revision; conflicts raise
    ``CorrectionConflictError``.
    """
    if await entities.resolve_canonical_entity_id(session, entity_id) != entity_id:
        raise _conflict("redirected_entity", "Use the canonical entity ID for corrections", entity_ids=[entity_id])
    closure = await _locked_closure(session, [entity_id])
    entity = next((item for item in closure.entity_rows if item.id == entity_id), None)
    if entity is None:
        raise _conflict("entity_missing", "Entity no longer exists", entity_ids=[entity_id])
    if entity.revision != payload.expected_revision:
        raise _conflict("stale_revision", "Entity changed; reload before suppressing candidates", entity_ids=[entity_id])
    by_id = {item.id: item for item in closure.memberships}
    if not set(payload.evidence_ids) <= set(by_id):
        raise _conflict("membership_unavailable", "Selected evidence is missing or belongs to another entity", entity_ids=[entity_id], membership_ids=payload.evidence_ids)
    selected = [by_id[item] for item in payload.evidence_ids]
    scoped = await _check_future_scope(payload.future_document_id, selected)
    now = datetime.now(UTC)
    for membership in selected:
        if membership.match_fingerprint is None:
            continue
        existing = await session.scalar(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "evidence",
            EntityCorrectionDecision.membership_id == membership.id,
        ).with_for_update())
        if existing is None:
            session.add(EntityCorrectionDecision(
                decision="suppress", scope="evidence", membership_id=membership.id,
                match_fingerprint=membership.match_fingerprint, actor_id=actor_id,
                reason=payload.reason, created_at=now,
            ))
        else:
            existing.decision, existing.entity_id = "suppress", None
            existing.match_fingerprint, existing.actor_id = membership.match_fingerprint, actor_id
            existing.reason, existing.created_at = payload.reason, now
    for fingerprint in sorted({item.match_fingerprint for item in scoped if item.match_fingerprint is not None}):
        prior = (await session.scalars(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "document",
            EntityCorrectionDecision.document_id == payload.future_document_id,
            EntityCorrectionDecision.match_fingerprint == fingerprint,
        ).order_by(EntityCorrectionDecision.created_at, EntityCorrectionDecision.id).with_for_update())).all()
        if len({(item.decision, item.entity_id) for item in prior}) > 1:
            raise _conflict("conflicting_owner_corrections", "Future selector already has conflicting owner rules", membership_ids=[item.id for item in scoped])
        if prior:
            rule = prior[-1]
            rule.decision, rule.entity_id = "suppress", None
            rule.actor_id, rule.reason, rule.created_at = actor_id, payload.reason, now
        else:
            session.add(EntityCorrectionDecision(
                decision="suppress", scope="document", document_id=payload.future_document_id,
                match_fingerprint=fingerprint, actor_id=actor_id, reason=payload.reason,
                created_at=now,
            ))
    await entities.record_owner_action(
        session, actor_id=actor_id, operation="entity_suppress", reason=payload.reason,
        affected_ids=[entity_id, *payload.evidence_ids], revisions={str(entity_id): entity.revision},
    )
    result = EntityCorrectionResult(
        operation="suppress", entity_id=entity_id, canonical_entity_id=entity_id,
        replacement_entity_ids=[], revision=entity.revision,
    )
    await _temporal_correction(session, selected, [(entity.id, entity.revision)], "suppression")
    await commit_with_replay(session, [make_graph_change(entity_id=entity_id)])
    return result


async def _temporal_before_relationships(session: AsyncSession, relationship_ids: list[UUID]) -> None:
    """Record actual pre-correction owner state before endpoint memberships move; history starts at this observation."""
    for relationship_id in relationship_ids:
        await relationships.record_relationship_history(session, relationship_id)


async def _temporal_correction(session: AsyncSession, memberships, entity_revisions: list[tuple[UUID, int]],
                               operation: str, *, deleted: bool = False) -> None:
    """Flush identifier-only desired-state changes with the canonical correction's existing atomic commit."""
    from modules.knowledge.temporal import public as temporal
    pairs = sorted({(member.document_version_id, member.chunk_id) for member in memberships}, key=str)
    for entity_id, revision in entity_revisions:
        await temporal.schedule_canonical_change(session, kind="entity", canonical_id=entity_id,
            revision=revision, fields=[operation], support=pairs, origin="owner", deleted=deleted)
