"""Relationship publication, creation and removal.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_graph_change
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships._common import (
    _admit,
    _build_evidence_rows,
    _evidence_read,
    _evidence_scope,
    _relationship_read,
    _schedule_relationship_change,
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
    RelationshipCreate,
    RelationshipRead,
)


async def _matching_membership_refs(
    session: AsyncSession,
    *,
    source_entity_id: UUID,
    target_entity_id: UUID,
    document_version_id: UUID,
    chunk_id: UUID,
    source_membership_id: UUID,
    target_membership_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Lock both endpoint memberships for write and require them to match the cited chunk.

    Raises ``ValueError`` when either membership belongs to a different entity, version or chunk.
    """
    memberships = await entities.get_membership_refs(
        session,
        [source_membership_id, target_membership_id],
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    by_id = {item.id: item for item in memberships}
    source_membership, target_membership = by_id[source_membership_id], by_id[target_membership_id]
    for item, entity_id in (
        (source_membership, source_entity_id),
        (target_membership, target_entity_id),
    ):
        if (
            item.entity_id != entity_id
            or item.document_version_id != document_version_id
            or item.chunk_id != chunk_id
        ):
            raise ValueError("Relationship evidence memberships do not match the cited chunk")


async def _upsert_extracted_support(
    session: AsyncSession,
    relationship: Relationship,
    ref: Any,
    *,
    document_version_id: UUID,
    chunk_id: UUID,
    source_membership_id: UUID,
    target_membership_id: UUID,
    confidence: float,
    scope: Scope,
) -> bool:
    """Insert or raise the exact support row for one extracted relationship and flush.

    The existing row is locked; confidence only ever increases. Returns whether the support set
    changed (new row or higher confidence) so the caller knows to record history.
    """
    evidence = await session.scalar(
        select(RelationshipEvidence)
        .where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == relationship.id,
            RelationshipEvidence.document_version_id == document_version_id,
            RelationshipEvidence.chunk_id == chunk_id,
            RelationshipEvidence.source_membership_id == source_membership_id,
            RelationshipEvidence.target_membership_id == target_membership_id,
        )
        .with_for_update()
    )
    support_changed = evidence is None or confidence > evidence.confidence
    if evidence is None:
        session.add(
            RelationshipEvidence(
                relationship_id=relationship.id,
                document_version_id=document_version_id,
                chunk_id=chunk_id,
                document_id=ref.document_id,
                source_id=ref.source_id,
                observed_at=ref.observed_at,
                confidence=confidence,
                source_membership_id=source_membership_id,
                target_membership_id=target_membership_id,
            )
        )
    else:
        evidence.confidence = max(evidence.confidence, confidence)
    await session.flush()
    return support_changed


async def publish_extracted_relationship(
    session: AsyncSession,
    *,
    source_entity_id: UUID,
    target_entity_id: UUID,
    relationship_type: str,
    document_version_id: UUID,
    chunk_id: UUID,
    source_membership_id: UUID,
    target_membership_id: UUID,
    confidence: float,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Publish exact extraction support, observed history and temporal desired state.

    Caller owns source/document fences and canonical transaction; no commit or
    external work. Stored observation history begins now, never at fact validity.
    """
    if source_entity_id == target_entity_id:
        return None
    refs = await documents.read_evidence_refs(
        session,
        [(document_version_id, chunk_id)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _matching_membership_refs(
        session,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        source_membership_id=source_membership_id,
        target_membership_id=target_membership_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    ref = refs[0]
    relationship = await session.scalar(
        select(Relationship)
        .where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.source_entity_id == source_entity_id,
            Relationship.target_entity_id == target_entity_id,
            Relationship.type == relationship_type,
            Relationship.origin == "derived",
            Relationship.valid_from.is_(None),
            Relationship.valid_to.is_(None),
        )
        .with_for_update()
    )
    created = relationship is None
    if relationship is None:
        relationship = Relationship(
            workspace_id=scope.workspace_id,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            type=relationship_type,
            origin="derived",
            confidence=confidence,
        )
        session.add(relationship)
        await session.flush()
    support_changed = await _upsert_extracted_support(
        session,
        relationship,
        ref,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        source_membership_id=source_membership_id,
        target_membership_id=target_membership_id,
        confidence=confidence,
        scope=scope,
    )
    supported_confidence = await session.scalar(
        select(func.max(RelationshipEvidence.confidence)).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == relationship.id,
        )
    )
    confidence_changed = (
        supported_confidence is not None and supported_confidence != relationship.confidence
    )
    if supported_confidence is not None:
        relationship.confidence = supported_confidence
    await session.flush()
    if created or support_changed or confidence_changed:
        await record_relationship_history(
            session, relationship.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        await _schedule_relationship_change(
            session,
            relationship.id,
            ["support", "confidence"],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return relationship.id


async def _lock_create_endpoints(
    session: AsyncSession,
    payload: RelationshipCreate,
    endpoints: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, Any]:
    """Lock the endpoint entities and cited memberships for write; return memberships by id.

    Endpoint failures (missing or terminal entity) surface as ``LookupError``; a missing evidence
    membership surfaces as ``ValueError``. Endpoints are locked before memberships.
    """
    try:
        await entities.get_entity_refs(
            session,
            endpoints,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except entities.TerminalEntityConflict as exc:
        raise LookupError("Relationship entity not found") from exc
    except LookupError as exc:
        raise LookupError("Relationship entity not found") from exc
    membership_ids = sorted(
        {
            identifier
            for item in payload.evidence
            for identifier in (item.source_membership_id, item.target_membership_id)
            if identifier is not None
        },
        key=str,
    )
    try:
        memberships = await entities.get_membership_refs(
            session,
            membership_ids,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        raise ValueError("Relationship endpoint evidence membership is missing") from exc
    memberships_by_id = {item.id: item for item in memberships}
    return memberships_by_id


def _new_relationship_row(payload: RelationshipCreate, scope: Scope) -> Relationship:
    """Build the unsaved relationship row for a create request.

    Pure: derived relationships take the maximum evidence confidence, owner ones the payload value.
    """
    relationship = Relationship(
        workspace_id=scope.workspace_id,
        source_entity_id=payload.source_entity_id,
        target_entity_id=payload.target_entity_id,
        type=payload.type,
        origin=payload.origin,
        confidence=(
            max((item.confidence for item in payload.evidence), default=None)
            if payload.origin == "derived"
            else payload.confidence
        ),
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        metadata_json=payload.metadata,
    )
    return relationship


async def create_relationship(
    session: AsyncSession,
    payload: RelationshipCreate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipRead:
    """Create and commit a relationship with exact endpoint/evidence support.

    The workspace owner scope is admitted under the access-fence lock first and
    records the audit actor. The input may be owner or derived origin. Derived relationships
    require both endpoint memberships for each evidence ref and use the maximum
    evidence confidence; the function locks write refs and commits audit plus
    graph notification, retained canonical snapshot and temporal desired state.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    if payload.source_entity_id == payload.target_entity_id:
        raise ValueError("Relationship endpoints must be different")
    if payload.origin == "derived" and not payload.evidence:
        raise ValueError("Derived relationships require at least one evidence reference")
    pairs = list(
        dict.fromkeys((item.document_version_id, item.chunk_id) for item in payload.evidence)
    )
    support_pairs = [
        (
            item.document_version_id,
            item.chunk_id,
            item.source_membership_id,
            item.target_membership_id,
        )
        for item in payload.evidence
    ]
    if len(set(support_pairs)) != len(support_pairs):
        raise ValueError("Relationship evidence membership pairs must be unique")
    evidence_refs = await documents.read_evidence_refs(
        session,
        pairs,
        for_write=bool(pairs),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in evidence_refs}
    endpoints = [payload.source_entity_id, payload.target_entity_id]
    memberships_by_id = await _lock_create_endpoints(
        session,
        payload,
        endpoints,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    evidence_rows = _build_evidence_rows(payload, by_pair, memberships_by_id)
    relationship = _new_relationship_row(payload, scope)
    session.add(relationship)
    await session.flush()
    for evidence in evidence_rows:
        evidence.relationship_id = relationship.id
    session.add_all(evidence_rows)
    await session.flush()
    evidence_read = await _evidence_read(
        session, evidence_rows, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    result = _relationship_read(relationship, evidence_read)
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_create",
        reason=payload.reason,
        affected_ids=[relationship.id, *endpoints],
    )
    await record_relationship_history(
        session, relationship.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await _schedule_relationship_change(
        session,
        relationship.id,
        ["created", "support"],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session,
        [make_graph_change(scope=scope, relationship_id=relationship.id)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return result


async def remove_relationship(
    session: AsyncSession,
    relationship_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    reason: str = "owner_relationship_delete",
) -> bool:
    """Delete a relationship after endpoint locks and commit its owner audit.

    The workspace owner scope is admitted under the access-fence lock first and
    is the audit actor. Returns False when the relationship is absent, rejects terminal
    endpoints, and captures exact support/history before deleting. Desired-state
    deletion, audit and replay commit atomically; no external graph work occurs.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    hint = await session.scalar(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.id == relationship_id,
        )
    )
    if hint is None:
        return False
    endpoints = [hint.source_entity_id, hint.target_entity_id]
    try:
        await entities.get_entity_refs(
            session,
            endpoints,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except entities.TerminalEntityConflict as exc:
        raise LookupError("Relationship entity not found") from exc
    relationship = await session.scalar(
        select(Relationship)
        .where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.id == relationship_id,
        )
        .with_for_update()
    )
    if relationship is None:
        return False
    await record_relationship_history(
        session,
        relationship_id,
        deleted=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _schedule_relationship_change(
        session,
        relationship_id,
        ["deleted"],
        deleted=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await session.execute(
        update(RelationshipSnapshotHistory)
        .where(
            RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
            RelationshipSnapshotHistory.relationship_id == relationship_id,
        )
        .values(state={}, support=[])
    )
    await session.delete(relationship)
    await entities.record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_delete",
        reason=reason,
        affected_ids=[relationship_id, *endpoints],
    )
    await commit_with_replay(
        session,
        [make_graph_change(scope=scope, relationship_id=relationship_id, deleted=True)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return True
