"""Merge/split request validation, locked closure, previews and entity merge."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import ReplayDraft, commit_with_replay, make_graph_change
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._corrections_common import (
    MAX_CORRECTION_MEMBERSHIPS,
    CorrectionConflictError,
    _admit,
    _Closure,
    _conflict,
    _temporal_before_relationships,
    _temporal_correction,
)
from modules.knowledge.entities._corrections_discover import _discover
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    EntityCorrectionConflict,
    EntityCorrectionPreview,
    EntityCorrectionResult,
    EntityMergeRequest,
    EntitySplitRequest,
    canonicalize_name,
)
from modules.knowledge.relationships import public as relationships
from modules.sources import public as sources
from modules.timeline import public as timeline


async def _validate_merge_request(
    source_id: UUID,
    payload: EntityMergeRequest,
    closure: _Closure,
) -> tuple[
    Entity, Entity, list[EntityEvidenceMembership], dict[str, EntityAlias], list[EntityAlias]
]:
    """Check revisions, types, protected fields, aliases, scope, and edge merge rules."""
    target_id = payload.into_id
    source = next((item for item in closure.entity_rows if item.id == source_id), None)
    target = next((item for item in closure.entity_rows if item.id == target_id), None)
    if source is None or target is None:
        raise _conflict(
            "entity_missing", "Merge entity no longer exists", entity_ids=[source_id, target_id]
        )
    if (
        source.revision != payload.expected_revision
        or target.revision != payload.expected_into_revision
    ):
        raise _conflict(
            "stale_revision",
            "An entity changed; reload before merging",
            entity_ids=[source_id, target_id],
        )
    if source.type != target.type:
        raise _conflict(
            "incompatible_entity_types",
            "Entities of different types cannot be merged",
            entity_ids=[source_id, target_id],
        )
    memberships = [item for item in closure.memberships if item.entity_id == source_id]
    await _check_future_scope(payload.future_document_id, memberships)
    target_aliases = {
        item.normalized_alias: item for item in closure.aliases if item.entity_id == target_id
    }
    source_aliases = [item for item in closure.aliases if item.entity_id == source_id]
    for alias in source_aliases:
        existing = target_aliases.get(alias.normalized_alias)
        if existing is not None and (
            existing.alias,
            existing.source_id,
            existing.confirmed,
            existing.origin,
        ) != (alias.alias, alias.source_id, alias.confirmed, alias.origin):
            raise _conflict(
                "protected_alias_conflict",
                "Merge has conflicting aliases; resolve them before merging",
                entity_ids=[source_id, target_id],
            )
        if existing is None and alias.origin == "owner":
            raise _conflict(
                "protected_alias_conflict",
                "Merge cannot transfer owner-authored aliases across entity identities",
                entity_ids=[source_id, target_id],
            )
    for field_name in ("name", "description"):
        source_value = getattr(source, field_name)
        source_origin = getattr(source, f"{field_name}_origin")
        target_value = getattr(target, field_name)
        target_origin = getattr(target, f"{field_name}_origin")
        if source_origin == "owner" and target_origin == "owner" and source_value != target_value:
            raise _conflict(
                "protected_field_conflict",
                f"Merge has conflicting owner-authored {field_name}",
                entity_ids=[source_id, target_id],
            )
        if (
            field_name == "description"
            and source_value
            and target_value
            and source_value != target_value
        ):
            raise _conflict(
                "derived_field_conflict",
                "Merge has two different descriptions; resolve the field before merging",
                entity_ids=[source_id, target_id],
            )
    try:
        redirect_ids = {row.old_entity_id for row in closure.redirect_rows}
        relationships.validate_entity_merge_plan(
            source_id, target_id, closure.relationship_refs, redirect_ids
        )
    except ValueError as exc:
        raise _conflict(
            "relationship_merge_conflict",
            str(exc),
            entity_ids=[source_id, target_id],
            relationship_ids=closure.relationship_ids,
        ) from exc
    return source, target, memberships, target_aliases, source_aliases


async def _validate_split_request(
    entity_id: UUID,
    payload: EntitySplitRequest,
    closure: _Closure,
) -> tuple[Entity, list[EntityEvidenceMembership]]:
    """Check revision, replacement identity, selected memberships, and edge split rules."""
    source = next((item for item in closure.entity_rows if item.id == entity_id), None)
    if source is None:
        raise _conflict("entity_missing", "Split entity no longer exists", entity_ids=[entity_id])
    if source.revision != payload.expected_revision:
        raise _conflict(
            "stale_revision", "Entity changed; reload before splitting", entity_ids=[entity_id]
        )
    if payload.new_entity.type != source.type:
        raise _conflict(
            "incompatible_entity_types",
            "Split entity must keep the original entity type",
            entity_ids=[entity_id],
        )
    if canonicalize_name(payload.new_entity.name) == source.canonical_name:
        raise _conflict(
            "split_identity_conflict",
            "Split entity must have a distinct canonical name",
            entity_ids=[entity_id],
        )
    by_id = {item.id: item for item in closure.memberships}
    selected_ids = set(payload.evidence_ids)
    if (
        not selected_ids
        or not selected_ids <= set(by_id)
        or any(by_id[item].entity_id != entity_id for item in selected_ids)
    ):
        raise _conflict(
            "membership_unavailable",
            "Selected evidence is missing, stale, or belongs to another entity",
            entity_ids=[entity_id],
            membership_ids=payload.evidence_ids,
        )
    selected = [by_id[item] for item in sorted(selected_ids, key=str)]
    await _check_future_scope(payload.future_document_id, selected)
    try:
        source_relationship_refs = [
            ref
            for ref in closure.relationship_refs
            if entity_id in (ref.source_entity_id, ref.target_entity_id)
        ]
        relationships.validate_entity_split_plan(entity_id, selected_ids, source_relationship_refs)
    except ValueError as exc:
        raise _conflict(
            "relationship_split_conflict",
            str(exc),
            entity_ids=[entity_id],
            relationship_ids=closure.relationship_ids,
        ) from exc
    return source, selected


async def _locked_closure(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    include_target_memberships: bool = False,
) -> _Closure:
    """Lock evidence owners and sorted graph rows, then reject any changed snapshot."""
    before = await _discover(
        session,
        entity_ids,
        scope=scope,
        include_target_memberships=include_target_memberships,
    )
    before_signature = before.signature()
    try:
        evidence_refs = await documents.read_evidence_refs(
            session,
            before.evidence_pairs,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "evidence_unavailable",
            "Correction evidence is no longer retained",
            entity_ids=before.entity_ids,
        ) from exc
    source_ids = sorted(
        set(before.source_ids) | {item.source_id for item in evidence_refs}, key=str
    )
    for source_id in source_ids:
        if (
            await sources.lock_source(
                session,
                source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            is None
        ):
            raise _conflict(
                "source_unavailable",
                "Correction source dependency is no longer retained",
                entity_ids=before.entity_ids,
            )
    document_ids = sorted(
        set(before.document_ids) | {item.document_id for item in evidence_refs}, key=str
    )
    if document_ids:
        await documents.lock_document_ids(
            session,
            document_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    try:
        verified_refs = await documents.read_evidence_refs(
            session,
            before.evidence_pairs,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "evidence_unavailable",
            "Correction evidence changed before locking",
            entity_ids=before.entity_ids,
        ) from exc
    if verified_refs != evidence_refs:
        raise _conflict(
            "evidence_unavailable",
            "Correction evidence changed before locking",
            entity_ids=before.entity_ids,
        )
    await session.scalars(
        select(Entity.id)
        .where(Entity.workspace_id == scope.workspace_id, Entity.id.in_(before.entity_ids))
        .order_by(Entity.id)
        .with_for_update()
        .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
        )
    try:
        await relationships.lock_correction_closure(
            session,
            before.relationship_entity_ids,
            set(before.relationship_ids),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "correction_closure_changed",
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
    after = await _discover(
        session, entity_ids, scope=scope, include_target_memberships=include_target_memberships
    )
    if before_signature != after.signature():
        raise _conflict(
            "correction_closure_changed",
            "Correction closure changed while locks were acquired; retry",
            entity_ids=before.entity_ids,
            membership_ids=[item.id for item in before.memberships],
            relationship_ids=before.relationship_ids,
        )
    memberships = sorted(
        {item.id for item in after.memberships}
        | {
            support.source_membership_id
            for ref in after.relationship_refs
            for support in ref.supports
            if support.source_membership_id is not None
        }
        | {
            support.target_membership_id
            for ref in after.relationship_refs
            for support in ref.supports
            if support.target_membership_id is not None
        }
        | {item.membership_id for item in after.alias_supports}
        | {item.membership_id for item in after.field_supports},
        key=str,
    )
    memberships = sorted(set(memberships) | set(after.membership_ids), key=str)
    if len(memberships) > MAX_CORRECTION_MEMBERSHIPS:
        raise _conflict(
            "correction_too_large",
            "Correction membership closure exceeds 200",
            entity_ids=after.entity_ids,
        )
    if memberships:
        await entities.get_membership_refs(
            session,
            memberships,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    verified = await _discover(
        session, entity_ids, scope=scope, include_target_memberships=include_target_memberships
    )
    if before_signature != verified.signature():
        raise _conflict(
            "correction_closure_changed",
            "Correction support closure changed while membership locks were acquired; retry",
            entity_ids=before.entity_ids,
            membership_ids=[item.id for item in before.memberships],
            relationship_ids=before.relationship_ids,
        )
    after = verified
    return after


async def preview_merge(
    session: AsyncSession,
    source_id: UUID,
    payload: EntityMergeRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityCorrectionPreview:
    """Return merge impact and conflicts without writing or locking a correction closure."""
    try:
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if source_id == payload.into_id:
            raise _conflict(
                "self_merge", "An entity cannot be merged into itself", entity_ids=[source_id]
            )
        if (
            await entities.resolve_canonical_entity_id(
                session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            != source_id
            or await entities.resolve_canonical_entity_id(
                session,
                payload.into_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            != payload.into_id
        ):
            raise _conflict(
                "redirected_entity",
                "Use canonical entity IDs for corrections",
                entity_ids=[source_id, payload.into_id],
            )
        closure = await _discover(
            session, [source_id, payload.into_id], scope=scope, include_target_memberships=True
        )
        await _validate_merge_request(source_id, payload, closure)
        return EntityCorrectionPreview(
            operation="merge",
            entity_ids=closure.entity_ids,
            membership_ids=[item.id for item in closure.memberships],
            relationship_ids=closure.relationship_ids,
            evidence_ref_count=len(closure.evidence_pairs),
        )
    except CorrectionConflictError as exc:
        return EntityCorrectionPreview(
            operation="merge",
            entity_ids=[source_id, payload.into_id],
            membership_ids=[],
            relationship_ids=[],
            evidence_ref_count=0,
            conflicts=[exc.conflict],
        )
    except (LookupError, ValueError) as exc:
        conflict = EntityCorrectionConflict(
            code="entity_missing"
            if isinstance(exc, LookupError)
            else "entity_identity_unavailable",
            message="An entity is missing, deleted, or has an unavailable redirect chain",
            entity_ids=[source_id, payload.into_id],
            membership_ids=[],
            relationship_ids=[],
        )
        return EntityCorrectionPreview(
            operation="merge",
            entity_ids=[source_id, payload.into_id],
            membership_ids=[],
            relationship_ids=[],
            evidence_ref_count=0,
            conflicts=[conflict],
        )


async def preview_split(
    session: AsyncSession,
    entity_id: UUID,
    payload: EntitySplitRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityCorrectionPreview:
    """Return split impact and conflicts without applying the requested correction."""
    try:
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
        closure = await _discover(session, [entity_id], scope=scope)
        await _validate_split_request(entity_id, payload, closure)
        return EntityCorrectionPreview(
            operation="split",
            entity_ids=closure.entity_ids,
            membership_ids=payload.evidence_ids,
            relationship_ids=closure.relationship_ids,
            evidence_ref_count=len(closure.evidence_pairs),
        )
    except CorrectionConflictError as exc:
        return EntityCorrectionPreview(
            operation="split",
            entity_ids=[entity_id],
            membership_ids=payload.evidence_ids,
            relationship_ids=[],
            evidence_ref_count=0,
            conflicts=[exc.conflict],
        )
    except (LookupError, ValueError) as exc:
        conflict = EntityCorrectionConflict(
            code="entity_missing"
            if isinstance(exc, LookupError)
            else "entity_identity_unavailable",
            message="Entity is missing, deleted, or has an unavailable redirect chain",
            entity_ids=[entity_id],
            membership_ids=[],
            relationship_ids=[],
        )
        return EntityCorrectionPreview(
            operation="split",
            entity_ids=[entity_id],
            membership_ids=payload.evidence_ids,
            relationship_ids=[],
            evidence_ref_count=0,
            conflicts=[conflict],
        )


async def _check_future_scope(
    document_id: UUID | None,
    memberships: list[EntityEvidenceMembership],
) -> list[EntityEvidenceMembership]:
    """Select evidence in the future document only when each row has a fingerprint."""
    if document_id is None:
        return []
    scoped = [item for item in memberships if item.document_id == document_id]
    if not scoped or any(item.match_fingerprint is None for item in scoped):
        raise _conflict(
            "future_scope_unavailable",
            "Future document scope requires selected evidence with a usable candidate fingerprint",
            membership_ids=[item.id for item in scoped],
        )
    return scoped


async def _record_assignments(
    session: AsyncSession,
    memberships: list[EntityEvidenceMembership],
    target_id: UUID,
    *,
    scope: Scope,
    actor_id: int,
    reason: str,
    future_document_id: UUID | None,
) -> None:
    """Upsert evidence- and optional document-scoped owner assignment decisions."""
    now = datetime.now(UTC)
    for membership in memberships:
        fingerprint = membership.match_fingerprint
        if fingerprint is None:
            continue
        old = await session.scalar(
            select(EntityCorrectionDecision)
            .where(
                EntityCorrectionDecision.workspace_id == scope.workspace_id,
                EntityCorrectionDecision.scope == "evidence",
                EntityCorrectionDecision.membership_id == membership.id,
            )
            .with_for_update()
        )
        if old is None:
            session.add(
                EntityCorrectionDecision(
                    workspace_id=scope.workspace_id,
                    decision="assign",
                    scope="evidence",
                    entity_id=target_id,
                    membership_id=membership.id,
                    match_fingerprint=fingerprint,
                    actor_id=actor_id,
                    reason=reason,
                    created_at=now,
                )
            )
        else:
            old.decision, old.entity_id = "assign", target_id
            old.match_fingerprint, old.actor_id, old.reason, old.created_at = (
                fingerprint,
                actor_id,
                reason,
                now,
            )
    scoped = await _check_future_scope(future_document_id, memberships)
    for fingerprint in sorted(
        {item.match_fingerprint for item in scoped if item.match_fingerprint is not None}
    ):
        prior = (
            await session.scalars(
                select(EntityCorrectionDecision)
                .where(
                    EntityCorrectionDecision.workspace_id == scope.workspace_id,
                    EntityCorrectionDecision.scope == "document",
                    EntityCorrectionDecision.document_id == future_document_id,
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
            rule.decision, rule.entity_id = "assign", target_id
            rule.actor_id, rule.reason, rule.created_at = actor_id, reason, now
        else:
            session.add(
                EntityCorrectionDecision(
                    workspace_id=scope.workspace_id,
                    decision="assign",
                    scope="document",
                    entity_id=target_id,
                    document_id=future_document_id,
                    match_fingerprint=fingerprint,
                    actor_id=actor_id,
                    reason=reason,
                    created_at=now,
                )
            )


async def _merge_identity_fields(
    session: AsyncSession, source: Entity, target: Entity, *, scope: Scope
) -> None:
    """Fold the source's name/description into the target by origin precedence (mutates both ORM rows).

    An owner-authored source value replaces a non-owner target value (dropping that field's derived support
    evidence); a derived source value only fills an empty target. Conflicting owner values were rejected by
    ``_validate_merge_request`` beforehand.
    """
    for field_name in ("name", "description"):
        source_origin = getattr(source, f"{field_name}_origin")
        target_origin = getattr(target, f"{field_name}_origin")
        source_value = getattr(source, field_name)
        if source_origin == "owner" and target_origin != "owner":
            setattr(target, field_name, source_value)
            setattr(target, f"{field_name}_origin", "owner")
            if field_name == "name":
                target.canonical_name = canonicalize_name(source_value) if source_value else None
            await session.execute(
                delete(EntityFieldEvidence).where(
                    EntityFieldEvidence.entity_id == target.id,
                    EntityFieldEvidence.field_name == field_name,
                    EntityFieldEvidence.membership_id.in_(
                        select(EntityEvidenceMembership.id).where(
                            EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        )
                    ),
                )
            )
        elif source_value and getattr(target, field_name) is None and source_origin == "derived":
            setattr(target, field_name, source_value)
            setattr(target, f"{field_name}_origin", "derived")
            if field_name == "name":
                target.canonical_name = canonicalize_name(source_value)


async def _merge_derived_name_alias(
    session: AsyncSession,
    source: Entity,
    target: Entity,
    target_aliases: dict[str, EntityAlias],
    memberships: list[EntityEvidenceMembership],
    closure: _Closure,
) -> None:
    """Keep a different derived source name as a derived target alias backed by the name's field evidence.

    Creates (and flushes) the alias when the target lacks it, records it in ``target_aliases`` and adds alias
    evidence for each source name-field support whose hash matches the source name.
    """
    source_derived_name = source.name if source.name_origin == "derived" else None
    if (
        source_derived_name
        and target.name
        and canonicalize_name(source_derived_name) != target.canonical_name
    ):
        normalized = canonicalize_name(source_derived_name)
        alias = target_aliases.get(normalized)
        if alias is None:
            alias = EntityAlias(
                entity_id=target.id,
                alias=source_derived_name,
                normalized_alias=normalized,
                confirmed=False,
                origin="derived",
                confidence=None,
            )
            session.add(alias)
            await session.flush()
            target_aliases[normalized] = alias
        membership_by_id = {item.id: item for item in memberships}
        name_hash = sha256(source_derived_name.encode("utf-8")).hexdigest()
        for support in closure.field_supports:
            membership = membership_by_id.get(support.membership_id)
            if (
                support.entity_id == source.id
                and support.field_name == "name"
                and support.value_hash == name_hash
                and membership is not None
            ):
                session.add(
                    EntityAliasEvidence(
                        alias_id=alias.id,
                        membership_id=membership.id,
                        confidence=membership.confidence,
                    )
                )


async def _merge_aliases(
    session: AsyncSession,
    closure: _Closure,
    source_aliases: list[EntityAlias],
    target_aliases: dict[str, EntityAlias],
    target: Entity,
    memberships: list[EntityEvidenceMembership],
) -> None:
    """Move source aliases to the target, or fold duplicates into the target's alias.

    A duplicate alias hands its supports to the target alias: a support on a new membership is re-pointed, a
    support on an already supported membership merges confidence (max) and is deleted. The duplicate alias is
    then deleted.
    """
    membership_by_id = {item.id: item for item in memberships}
    for alias in source_aliases:
        existing = target_aliases.get(alias.normalized_alias)
        if existing is None:
            for alias_support in closure.alias_supports:
                if (
                    alias_support.alias_id == alias.id
                    and alias_support.membership_id in membership_by_id
                ):
                    alias_support.alias_id = alias.id
            alias.entity_id = target.id
            continue
        supports = [item for item in closure.alias_supports if item.alias_id == alias.id]
        target_supports = {
            item.membership_id: item
            for item in closure.alias_supports
            if item.alias_id == existing.id
        }
        for alias_support in supports:
            target_support = target_supports.get(alias_support.membership_id)
            if target_support is None:
                alias_support.alias_id = existing.id
            else:
                target_support.confidence = max(target_support.confidence, alias_support.confidence)
                await session.delete(alias_support)
        await session.delete(alias)


async def _merge_field_supports(
    session: AsyncSession, closure: _Closure, source: Entity, target: Entity
) -> None:
    """Re-point the source's derived field supports to the target when the target's derived value matches.

    A support whose hash no longer matches the target's current derived name/description is deleted.
    """
    for support in closure.field_supports:
        if support.entity_id != source.id:
            continue
        value = target.name if support.field_name == "name" else target.description
        value_origin = (
            target.name_origin if support.field_name == "name" else target.description_origin
        )
        if (
            value_origin == "derived"
            and value is not None
            and support.value_hash == sha256(value.encode("utf-8")).hexdigest()
        ):
            support.entity_id = target.id
        else:
            await session.delete(support)


async def merge_entity(
    session: AsyncSession,
    source_id: UUID,
    payload: EntityMergeRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityCorrectionResult:
    """Merge two canonical entities and commit their audited support migration.

    Authorization is enforced by the owner-write route; ``actor_id`` is audit
    provenance. The function locks/revalidates a bounded closure, redirects the
    source identity to the target, preserves evidence/document assignment rules,
    and rebinds only relationships whose support remains valid. Conflicts raise
    ``CorrectionConflictError`` and successful changes commit with graph events.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    actor_id = _actor(scope)
    if source_id == payload.into_id:
        raise _conflict(
            "self_merge", "An entity cannot be merged into itself", entity_ids=[source_id]
        )
    if (
        await entities.resolve_canonical_entity_id(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != source_id
        or await entities.resolve_canonical_entity_id(
            session, payload.into_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != payload.into_id
    ):
        raise _conflict(
            "redirected_entity",
            "Use canonical entity IDs for corrections",
            entity_ids=[source_id, payload.into_id],
        )
    closure = await _locked_closure(
        session,
        [source_id, payload.into_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
        include_target_memberships=True,
    )
    await _temporal_before_relationships(
        session,
        closure.relationship_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source, target, memberships, target_aliases, source_aliases = await _validate_merge_request(
        source_id, payload, closure
    )
    previous_revisions: dict[str, int | None] = {
        str(source.id): source.revision,
        str(target.id): target.revision,
    }
    await _merge_identity_fields(session, source, target, scope=scope)
    await _merge_derived_name_alias(session, source, target, target_aliases, memberships, closure)
    await _merge_aliases(session, closure, source_aliases, target_aliases, target, memberships)
    await _record_assignments(
        session,
        memberships,
        target.id,
        scope=scope,
        actor_id=actor_id,
        reason=payload.reason,
        future_document_id=payload.future_document_id,
    )
    for membership in memberships:
        membership.entity_id = target.id
    await _merge_field_supports(session, closure, source, target)
    try:
        replacements = await relationships.apply_entity_merge(
            session,
            source.id,
            target.id,
            set(closure.relationship_ids),
            closure.relationship_entity_ids,
            {row.old_entity_id for row in closure.redirect_rows},
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError as exc:
        raise _conflict(
            "relationship_merge_conflict",
            str(exc),
            entity_ids=[source.id, target.id],
            relationship_ids=closure.relationship_ids,
        ) from exc
    changed_timeline_ids = await timeline.apply_entity_merge(
        session,
        source_id=source.id,
        target_id=target.id,
        event_ids=closure.timeline_event_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    for redirect in closure.redirect_rows:
        redirect.target_entity_id = target.id
    session.add(
        EntityRedirect(
            workspace_id=scope.workspace_id,
            old_entity_id=source.id,
            target_entity_id=target.id,
            actor_id=actor_id,
            reason=" ".join(payload.reason.split()),
            created_at=datetime.now(UTC),
        )
    )
    if source.name_origin == "derived":
        source.name = source.canonical_name = None
        source.name_origin = None
    if source.description_origin == "derived":
        source.description = None
        source.description_origin = None
    source.revision += 1
    target.revision += 1
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_merge",
        reason=payload.reason,
        affected_ids=[
            source.id,
            target.id,
            *[row.old_entity_id for row in closure.redirect_rows],
            *[old for old, _ in replacements],
        ],
        revisions=previous_revisions,
    )
    await session.flush()
    result = EntityCorrectionResult(
        operation="merge",
        entity_id=source.id,
        canonical_entity_id=target.id,
        replacement_entity_ids=sorted({new for _, new in replacements}, key=str),
        revision=target.revision,
    )
    drafts: list[ReplayDraft] = [make_graph_change(entity_id=target.id, scope=scope)]
    drafts.extend(make_graph_change(relationship_id=new, scope=scope) for _, new in replacements)
    drafts.extend(
        await timeline.revise_corrected_events(
            session,
            changed_timeline_ids,
            entity_id=target.id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    await _temporal_correction(
        session,
        closure.memberships,
        [(source.id, source.revision), (target.id, target.revision)],
        "merge",
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
