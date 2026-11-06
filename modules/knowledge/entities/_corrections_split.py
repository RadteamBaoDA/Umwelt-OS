"""Entity split, candidate suppression and temporal correction helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.realtime import ReplayDraft, commit_with_replay, make_graph_change
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import Scope
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._corrections_common import (
    _admit,
    _Closure,
    _conflict,
    _temporal_before_relationships,
    _temporal_correction,
)
from modules.knowledge.entities._corrections_merge import (
    _check_future_scope,
    _locked_closure,
    _record_assignments,
    _validate_split_request,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityFieldEvidence,
)
from modules.knowledge.entities.schemas import (
    EntityCorrectionResult,
    EntitySplitRequest,
    EntitySuppressionRequest,
    canonicalize_name,
)
from modules.knowledge.relationships import public as relationships
from modules.timeline import public as timeline


async def _split_alias_supports(
    session: AsyncSession,
    closure: _Closure,
    selected_ids: set[UUID],
    new_aliases: list[EntityAlias],
    new_id: UUID,
) -> list[EntityAliasEvidence]:
    """Move alias evidence on the selected memberships to aliases of the new entity.

    Owner-authored aliases lose the selected support (they stay with the source); derived aliases are cloned
    onto the new entity (reusing an existing new alias with the same normalized text) and the support is
    re-pointed. Appends to ``new_aliases``, flushes after each created alias, and returns the selected supports.
    """
    selected_supports = [
        item for item in closure.alias_supports if item.membership_id in selected_ids
    ]
    for support in selected_supports:
        alias = next(item for item in closure.aliases if item.id == support.alias_id)
        if alias.origin == "owner":
            await session.delete(support)
            continue
        existing = next(
            (item for item in new_aliases if item.normalized_alias == alias.normalized_alias), None
        )
        if existing is None:
            existing = EntityAlias(
                entity_id=new_id,
                alias=alias.alias,
                normalized_alias=alias.normalized_alias,
                source_id=alias.source_id,
                confirmed=alias.confirmed,
                origin="derived",
                confidence=alias.confidence,
            )
            session.add(existing)
            new_aliases.append(existing)
            await session.flush()
        support.alias_id = existing.id
    return selected_supports


async def _split_move_evidence(
    session: AsyncSession,
    closure: _Closure,
    selected: list[EntityEvidenceMembership],
    selected_ids: set[UUID],
    selected_supports: list[EntityAliasEvidence],
    entity_id: UUID,
    new_id: UUID,
) -> None:
    """Re-own the selected memberships and prune support the source can no longer justify.

    Selected derived field supports are deleted (the new identity fields are owner-authored); a derived source
    alias is deleted when all its remaining evidence was moved away by the selection.
    """
    for item in selected:
        item.entity_id = new_id
    # New identity fields are owner-authored; selected derived support cannot override them.
    for field_support in closure.field_supports:
        if field_support.membership_id in selected_ids:
            await session.delete(field_support)
    for alias in closure.aliases:
        if alias.entity_id != entity_id or alias.origin != "derived":
            continue
        remaining = (
            await session.scalars(
                select(EntityAliasEvidence.id).where(EntityAliasEvidence.alias_id == alias.id)
            )
        ).all()
        if not remaining and any(item.alias_id == alias.id for item in selected_supports):
            await session.delete(alias)


async def _split_clear_source_fields(
    session: AsyncSession,
    closure: _Closure,
    source: Entity,
    entity_id: UUID,
    selected_ids: set[UUID],
) -> None:
    """Clear a derived name/description of the source that lost its last field evidence to the selection."""
    for field_name in ("name", "description"):
        if getattr(source, f"{field_name}_origin") != "derived":
            continue
        remaining = await session.scalar(
            select(EntityFieldEvidence.id)
            .where(
                EntityFieldEvidence.entity_id == entity_id,
                EntityFieldEvidence.field_name == field_name,
            )
            .limit(1)
        )
        if remaining is None and any(
            item.entity_id == entity_id
            and item.field_name == field_name
            and item.membership_id in selected_ids
            for item in closure.field_supports
        ):
            setattr(source, field_name, None)
            setattr(source, f"{field_name}_origin", None)
            if field_name == "name":
                source.canonical_name = None


async def split_entity(
    session: AsyncSession,
    entity_id: UUID,
    payload: EntitySplitRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityCorrectionResult:
    """Move selected evidence into a new owner-authored entity and commit the audit.

    The owner-write route authorizes the operation and supplies ``actor_id`` for
    provenance. After locking and validating the bounded closure, it moves chosen
    memberships and supported relationships; new identity fields remain
    owner-authored rather than inheriting derived field support. Conflicts raise
    ``CorrectionConflictError``.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    actor_id = _actor(scope)
    if (
        await entities.resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != entity_id
    ):
        raise _conflict(
            "redirected_entity",
            "Use the canonical entity ID for corrections",
            entity_ids=[entity_id],
        )
    closure = await _locked_closure(
        session,
        [entity_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    source, selected = await _validate_split_request(entity_id, payload, closure)
    await _temporal_before_relationships(
        session,
        closure.relationship_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    selected_ids = set(payload.evidence_ids)
    new_id = uuid7()
    new_entity = Entity(
        workspace_id=scope.workspace_id,
        id=new_id,
        type=payload.new_entity.type,
        name=payload.new_entity.name,
        canonical_name=canonicalize_name(payload.new_entity.name),
        description=payload.new_entity.description,
        name_origin="owner",
        description_origin="owner" if payload.new_entity.description is not None else None,
        metadata_json=payload.new_entity.metadata,
        revision=1,
    )
    session.add(new_entity)
    await session.flush()
    new_aliases = [
        EntityAlias(
            entity_id=new_id,
            alias=value,
            normalized_alias=canonicalize_name(value),
            confirmed=True,
            origin="owner",
        )
        for value in payload.new_entity.aliases
        if canonicalize_name(value) != new_entity.canonical_name
    ]
    session.add_all(new_aliases)
    await session.flush()
    previous_revision = source.revision
    await _record_assignments(
        session,
        selected,
        new_id,
        scope=scope,
        actor_id=actor_id,
        reason=payload.reason,
        future_document_id=payload.future_document_id,
    )
    selected_supports = await _split_alias_supports(
        session, closure, selected_ids, new_aliases, new_id
    )
    await _split_move_evidence(
        session, closure, selected, selected_ids, selected_supports, entity_id, new_id
    )
    try:
        source_relationship_ids = {
            ref.id
            for ref in closure.relationship_refs
            if entity_id in (ref.source_entity_id, ref.target_entity_id)
        }
        replacements = await relationships.apply_entity_split(
            session,
            entity_id,
            new_id,
            selected_ids,
            source_relationship_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "relationship_split_conflict",
            str(exc),
            entity_ids=[entity_id],
            relationship_ids=closure.relationship_ids,
        ) from exc
    changed_timeline_ids = await timeline.apply_entity_split(
        session,
        source_id=entity_id,
        target_id=new_id,
        event_ids=closure.timeline_event_ids,
        selected_pairs={(item.document_version_id, item.chunk_id) for item in selected},
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _split_clear_source_fields(session, closure, source, entity_id, selected_ids)
    source.revision += 1
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_split",
        reason=payload.reason,
        affected_ids=[source.id, new_id, *selected_ids, *[old for old, _ in replacements]],
        revisions={str(source.id): previous_revision, str(new_id): 1},
    )
    await session.flush()
    result = EntityCorrectionResult(
        operation="split",
        entity_id=source.id,
        canonical_entity_id=source.id,
        replacement_entity_ids=[new_id],
        revision=source.revision,
    )
    drafts: list[ReplayDraft] = [
        make_graph_change(entity_id=source.id, scope=scope),
        make_graph_change(entity_id=new_id, scope=scope),
    ]
    drafts.extend(make_graph_change(relationship_id=new, scope=scope) for _, new in replacements)
    drafts.extend(
        await timeline.revise_corrected_events(
            session,
            changed_timeline_ids,
            entity_id=source.id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    await _temporal_correction(
        session,
        closure.memberships,
        [(source.id, source.revision), (new_entity.id, new_entity.revision)],
        "split",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return result


async def suppress_candidates(
    session: AsyncSession,
    entity_id: UUID,
    payload: EntitySuppressionRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityCorrectionResult:
    """Commit owner suppression rules for selected evidence and future documents.

    Authorization is enforced by the owner-write route; ``actor_id`` and reason
    are recorded in the audit. The operation requires a current canonical
    revision and records scoped decisions without deleting memberships/evidence
    or incrementing the entity revision; conflicts raise
    ``CorrectionConflictError``.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    actor_id = _actor(scope)
    if (
        await entities.resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != entity_id
    ):
        raise _conflict(
            "redirected_entity",
            "Use the canonical entity ID for corrections",
            entity_ids=[entity_id],
        )
    closure = await _locked_closure(
        session,
        [entity_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    entity = next((item for item in closure.entity_rows if item.id == entity_id), None)
    if entity is None:
        raise _conflict("entity_missing", "Entity no longer exists", entity_ids=[entity_id])
    if entity.revision != payload.expected_revision:
        raise _conflict(
            "stale_revision",
            "Entity changed; reload before suppressing candidates",
            entity_ids=[entity_id],
        )
    by_id = {item.id: item for item in closure.memberships}
    if not set(payload.evidence_ids) <= set(by_id):
        raise _conflict(
            "membership_unavailable",
            "Selected evidence is missing or belongs to another entity",
            entity_ids=[entity_id],
            membership_ids=payload.evidence_ids,
        )
    selected = [by_id[item] for item in payload.evidence_ids]
    scoped = await _check_future_scope(payload.future_document_id, selected)
    now = datetime.now(UTC)
    for membership in selected:
        if membership.match_fingerprint is None:
            continue
        existing = await session.scalar(
            select(EntityCorrectionDecision)
            .where(
                EntityCorrectionDecision.workspace_id == scope.workspace_id,
                EntityCorrectionDecision.scope == "evidence",
                EntityCorrectionDecision.membership_id == membership.id,
            )
            .with_for_update()
        )
        if existing is None:
            session.add(
                EntityCorrectionDecision(
                    workspace_id=scope.workspace_id,
                    decision="suppress",
                    scope="evidence",
                    membership_id=membership.id,
                    match_fingerprint=membership.match_fingerprint,
                    actor_id=actor_id,
                    reason=payload.reason,
                    created_at=now,
                )
            )
        else:
            existing.decision, existing.entity_id = "suppress", None
            existing.match_fingerprint, existing.actor_id = membership.match_fingerprint, actor_id
            existing.reason, existing.created_at = payload.reason, now
    for fingerprint in sorted(
        {item.match_fingerprint for item in scoped if item.match_fingerprint is not None}
    ):
        prior = (
            await session.scalars(
                select(EntityCorrectionDecision)
                .where(
                    EntityCorrectionDecision.workspace_id == scope.workspace_id,
                    EntityCorrectionDecision.scope == "document",
                    EntityCorrectionDecision.document_id == payload.future_document_id,
                    EntityCorrectionDecision.match_fingerprint == fingerprint,
                )
                .order_by(EntityCorrectionDecision.created_at, EntityCorrectionDecision.id)
                .with_for_update()
            )
        ).all()
        if len({(item.decision, item.entity_id) for item in prior}) > 1:
            raise _conflict(
                "conflicting_owner_corrections",
                "Future selector already has conflicting owner rules",
                membership_ids=[item.id for item in scoped],
            )
        if prior:
            rule = prior[-1]
            rule.decision, rule.entity_id = "suppress", None
            rule.actor_id, rule.reason, rule.created_at = actor_id, payload.reason, now
        else:
            session.add(
                EntityCorrectionDecision(
                    workspace_id=scope.workspace_id,
                    decision="suppress",
                    scope="document",
                    document_id=payload.future_document_id,
                    match_fingerprint=fingerprint,
                    actor_id=actor_id,
                    reason=payload.reason,
                    created_at=now,
                )
            )
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_suppress",
        reason=payload.reason,
        affected_ids=[entity_id, *payload.evidence_ids],
        revisions={str(entity_id): entity.revision},
    )
    result = EntityCorrectionResult(
        operation="suppress",
        entity_id=entity_id,
        canonical_entity_id=entity_id,
        replacement_entity_ids=[],
        revision=entity.revision,
    )
    await _temporal_correction(
        session,
        selected,
        [(entity.id, entity.revision)],
        "suppression",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session,
        [make_graph_change(entity_id=entity_id, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return result
