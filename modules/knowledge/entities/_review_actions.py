"""Owner review actions: assigning entity candidates and resolving relationship reviews.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.realtime import commit_with_replay, make_graph_change
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities._decisions import get_document_correction_decisions
from modules.knowledge.entities._extraction import record_extraction_membership
from modules.knowledge.entities._reads import (
    get_entity_refs,
    get_membership_refs,
    record_owner_action,
)
from modules.knowledge.entities._review_list import (
    _review_entity_bindings,
    _review_hints,
    _review_snapshot_digest,
)
from modules.knowledge.entities.models import (
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityExtractionResult,
    EntityExtractionWork,
)
from modules.knowledge.entities.schemas import (
    EntityRelationshipReviewRequest,
    EntityRelationshipReviewResult,
    EntityReviewAssignmentRequest,
    EntityReviewAssignmentResult,
)
from modules.sources import public as sources


async def _lock_review_owner_source(
    session: AsyncSession,
    work_hint: EntityExtractionWork,
    expected_generation: int,
    fence: AccessFence,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    removed_message: str,
    changed_message: str,
) -> tuple[UUID, UUID, Any]:
    """Locate the reviewed version's document/source and lock the Source against the caller's access fence.

    Raises ``LookupError(removed_message)`` when the evidence locator is gone and
    ``ValueError(changed_message)`` when the Source is missing or its generation differs from
    ``expected_generation``. Returns ``(document_id, source_id, source_fence)``.
    """
    from modules.knowledge.documents import public as documents

    locator = await documents.review_version_locator(
        session,
        work_hint.document_version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if locator is None:
        raise LookupError(removed_message)
    document_id, source_id = locator
    source = await sources.lock_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    if source is None or source.generation != expected_generation:
        raise ValueError(changed_message)
    return document_id, source_id, source


async def _lock_review_extraction(
    session: AsyncSession,
    work_id: UUID,
    result_id: UUID,
    scope: Scope,
) -> tuple[EntityExtractionWork | None, EntityExtractionResult | None]:
    """Re-read the work row then the result row FOR UPDATE (that order) with fresh state (populate_existing)."""
    work = await session.scalar(
        select(EntityExtractionWork)
        .where(
            EntityExtractionWork.id == work_id,
            EntityExtractionWork.workspace_id == scope.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.scalar(
        select(EntityExtractionResult)
        .where(
            EntityExtractionResult.id == result_id,
            EntityExtractionResult.work_id == work_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return work, result


def _set_review_decision(
    result: EntityExtractionResult, candidate_id: UUID, state: dict[str, Any]
) -> None:
    """Replace ``result.review_json`` with a deep copy whose matching candidate carries ``decision_state``.

    A new list object is assigned so the JSON column change is detected by the ORM.
    """
    updated_review = deepcopy(result.review_json)
    for item in updated_review:
        if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id):
            item["decision_state"] = state
            break
    result.review_json = updated_review


def _assign_selector(snapshot: dict[str, Any]) -> tuple[str, str, str, list[UUID], float]:
    """Parse and bound-check the evidence selector of an entity review snapshot (pure).

    Returns ``(candidate_type, candidate_key, fingerprint, chunk_ids, confidence)``; raises ``ValueError``
    when fields are missing/unparseable or chunk/fingerprint/key sizes are invalid.
    """
    try:
        candidate_type = str(snapshot["candidate_type"])
        candidate_key = str(snapshot["candidate_key"])
        fingerprint = str(snapshot["match_fingerprint"])
        chunks = [UUID(str(value)) for value in snapshot["chunk_ids"]]
        confidence = float(snapshot["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Review candidate evidence selector is unavailable") from exc
    if (
        not 1 <= len(chunks) <= 5
        or len(set(chunks)) != len(chunks)
        or len(fingerprint) != 64
        or len(candidate_key) != 64
    ):
        raise ValueError("Review candidate evidence selector is invalid")
    return candidate_type, candidate_key, fingerprint, chunks, confidence


async def _record_assign_decisions(
    session: AsyncSession,
    membership_ids: list[UUID],
    payload: EntityReviewAssignmentRequest,
    fingerprint: str,
    actor_id: int,
    scope: Scope,
) -> None:
    """Add an ``assign`` correction decision per membership, locking any existing decision row first.

    Raises ``ValueError`` when a membership already has a conflicting owner decision. Adds rows to the
    session without flushing or committing.
    """
    for membership_id in membership_ids:
        existing = await session.scalar(
            select(EntityCorrectionDecision)
            .where(
                EntityCorrectionDecision.workspace_id == scope.workspace_id,
                EntityCorrectionDecision.scope == "evidence",
                EntityCorrectionDecision.membership_id == membership_id,
            )
            .with_for_update()
        )
        if existing is None:
            session.add(
                EntityCorrectionDecision(
                    workspace_id=scope.workspace_id,
                    decision="assign",
                    scope="evidence",
                    entity_id=payload.target_entity_id,
                    membership_id=membership_id,
                    match_fingerprint=fingerprint,
                    actor_id=actor_id,
                    reason=payload.reason,
                    created_at=datetime.now(UTC),
                )
            )
        elif existing.decision != "assign" or existing.entity_id != payload.target_entity_id:
            raise ValueError("Evidence membership already has a conflicting owner decision")


def _candidate_snapshots(review_json: object, candidate_id: UUID) -> list[dict[str, Any]]:
    """Return the review items whose ``candidate_id`` equals ``candidate_id`` (non-list review is empty)."""
    reviews = review_json if isinstance(review_json, list) else []
    return [
        item
        for item in reviews
        if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)
    ]


def _assign_snapshot(
    result_hint: EntityExtractionResult,
    candidate_id: UUID,
    payload: EntityReviewAssignmentRequest,
) -> tuple[list[Any], dict[str, Any]]:
    """Select and validate the entity review snapshot an assignment targets (pure).

    Returns the full review list (for selector-set binding) and the matching snapshot. Raises
    ``LookupError`` if the candidate is gone, ``ValueError`` if the digest changed, a decision already exists
    or a future-document scope is requested.
    """
    reviews = result_hint.review_json if isinstance(result_hint.review_json, list) else []
    candidates = _candidate_snapshots(reviews, candidate_id)
    if len(candidates) != 1:
        raise LookupError("Review candidate is no longer available")
    snapshot = candidates[0]
    if (
        snapshot.get("kind") != "entity"
        or snapshot.get("snapshot_digest") != payload.snapshot_digest
        or _review_snapshot_digest(snapshot) != payload.snapshot_digest
    ):
        raise ValueError("Review candidate snapshot changed; reload the queue")
    if snapshot.get("decision_state"):
        raise ValueError("Review candidate was already resolved")
    if payload.future_document_id is not None:
        raise ValueError("Future document scope is not available from this review action")
    return reviews, snapshot


async def _assign_prior_and_target(
    session: AsyncSession,
    candidate_id: UUID,
    payload: EntityReviewAssignmentRequest,
    *,
    document_id: UUID,
    version_id: UUID,
    bindings: dict[str, tuple[str, set[UUID]]],
    candidate_type: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[UUID, str, UUID | None] | None, Any]:
    """Read the prior correction decision and lock the target entity for write.

    Raises ``ValueError`` if a conflicting decision exists or the target's canonical id, type or revision
    differs from the request. Returns ``(prior_decision, target_ref)``.
    """
    prior = await get_document_correction_decisions(
        session,
        document_id,
        version_id,
        bindings,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    prior_decision = prior.get(str(candidate_id))
    if prior_decision is not None and (
        prior_decision[1] != "assign" or prior_decision[2] != payload.target_entity_id
    ):
        raise ValueError("A conflicting correction decision already exists")
    refs = await get_entity_refs(
        session,
        [payload.target_entity_id],
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    target = refs[0]
    if (
        target.canonical_id != payload.target_entity_id
        or target.type != candidate_type
        or target.revision != payload.expected_target_revision
    ):
        raise ValueError("Target entity changed or has an incompatible identity")
    return prior_decision, target


async def assign_review_candidate(
    session: AsyncSession,
    candidate_id: UUID,
    payload: EntityReviewAssignmentRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    actor_id: int,
) -> EntityReviewAssignmentResult:
    """Bind one durable extraction candidate to an owner-selected canonical entity.

    Lock order: access fence (admission) -> Source -> document evidence -> target entity -> extraction work
    and result rows (FOR UPDATE) -> evidence again -> correction decisions (FOR UPDATE). Every stale snapshot,
    selector or decision change raises ``ValueError``/``LookupError`` before any write; success commits once
    via ``commit_with_replay``.
    """
    from modules.knowledge.documents import public as documents

    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    if actor_id != _actor(scope):
        raise ForbiddenError("Review actor does not match the admitted owner")
    result_hint, work_hint = await _review_hints(session, payload.result_id, scope)
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    reviews, snapshot = _assign_snapshot(result_hint, candidate_id, payload)
    candidate_type, candidate_key, fingerprint, chunks, confidence = _assign_selector(snapshot)

    document_id, source_id, source = await _lock_review_owner_source(
        session,
        work_hint,
        payload.expected_owner_generation,
        fence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        removed_message="Review evidence was removed",
        changed_message="Owner evidence fence changed; reload the review candidate",
    )
    evidence = await documents.lock_review_version_evidence(
        session,
        document_id=document_id,
        source_id=source_id,
        version_id=work_hint.document_version_id,
        source_generation=source.generation,
        chunk_ids=chunks,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")

    bindings = _review_entity_bindings(reviews)
    if str(candidate_id) not in bindings or bindings[str(candidate_id)] != (
        fingerprint,
        set(chunks),
    ):
        raise ValueError("Selected candidate is missing from the complete result selector set")
    prior_decision, target = await _assign_prior_and_target(
        session,
        candidate_id,
        payload,
        document_id=document_id,
        version_id=work_hint.document_version_id,
        bindings=bindings,
        candidate_type=candidate_type,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )

    work, result = await _lock_review_extraction(session, work_hint.id, payload.result_id, scope)
    if (
        work is None
        or result is None
        or work.status != "succeeded"
        or work.document_version_id != extraction_version_id
        or work.source_generation != extraction_generation
        or work.id != extraction_work_id
    ):
        raise ValueError("Review extraction changed; reload the queue")
    current_snapshot = _candidate_snapshots(result.review_json, candidate_id)
    if (
        len(current_snapshot) != 1
        or current_snapshot[0].get("snapshot_digest") != payload.snapshot_digest
        or _review_snapshot_digest(current_snapshot[0]) != payload.snapshot_digest
        or current_snapshot[0].get("decision_state")
    ):
        raise ValueError("Review candidate snapshot changed or was resolved")
    evidence = await documents.lock_review_version_evidence(
        session,
        document_id=document_id,
        source_id=source_id,
        version_id=work.document_version_id,
        source_generation=payload.expected_owner_generation,
        chunk_ids=chunks,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")
    current_bindings = _review_entity_bindings(
        result.review_json if isinstance(result.review_json, list) else []
    )
    if current_bindings != bindings:
        raise ValueError("Same-result candidate selectors changed; reload the review")
    current = await get_document_correction_decisions(
        session,
        document_id,
        work.document_version_id,
        current_bindings,
        for_update=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    decision = current.get(str(candidate_id))
    if decision != prior_decision:
        raise ValueError("Correction decision changed; reload the review")

    memberships = [
        await record_extraction_membership(
            session,
            entity_id=payload.target_entity_id,
            evidence_ref=documents.ExtractionEvidenceRef(
                document_id=ref.document_id,
                document_version_id=ref.document_version_id,
                source_id=ref.source_id,
                source_generation=ref.current_source_generation,
                chunk_id=ref.chunk_id,
            ),
            source_generation=payload.expected_owner_generation,
            extraction_identity=str(work.id),
            candidate_key=candidate_key,
            match_fingerprint=fingerprint,
            observed_at=ref.observed_at,
            confidence=confidence,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        for ref in evidence
    ]
    await _record_assign_decisions(session, memberships, payload, fingerprint, actor_id, scope)
    _set_review_decision(
        result,
        candidate_id,
        {
            "kind": "assigned",
            "target_entity_id": str(payload.target_entity_id),
            "membership_ids": [str(value) for value in memberships],
        },
    )
    await record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_review_assign",
        reason=payload.reason,
        affected_ids=[payload.target_entity_id, candidate_id, *memberships],
        revisions={str(payload.target_entity_id): target.revision},
    )
    await session.flush()
    await commit_with_replay(
        session,
        [make_graph_change(entity_id=payload.target_entity_id, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return EntityReviewAssignmentResult(
        candidate_id=candidate_id,
        target_entity_id=payload.target_entity_id,
        membership_ids=memberships,
        revision=target.revision,
    )


async def _relationship_endpoint_memberships(
    session: AsyncSession,
    *,
    scope: Scope,
    work_id: UUID,
    document_version_id: UUID,
    chunk_id: UUID,
    source_key: str,
    target_key: str,
) -> tuple[EntityEvidenceMembership, EntityEvidenceMembership]:
    """Require one distinct endpoint membership per candidate on the cited chunk.

    Reads at most 3 rows ordered by (entity_id, id); raises ``ValueError`` unless each candidate key has
    exactly one membership and the two resolve to different memberships and entities.
    """
    rows = list(
        (
            await session.scalars(
                select(EntityEvidenceMembership)
                .where(
                    EntityEvidenceMembership.workspace_id == scope.workspace_id,
                    EntityEvidenceMembership.extraction_identity == str(work_id),
                    EntityEvidenceMembership.candidate_key.in_([source_key, target_key]),
                    EntityEvidenceMembership.document_version_id == document_version_id,
                    EntityEvidenceMembership.chunk_id == chunk_id,
                )
                .order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id)
                .limit(3)
            )
        ).all()
    )
    left = [row for row in rows if row.candidate_key == source_key]
    right = [row for row in rows if row.candidate_key == target_key]
    if (
        len(left) != 1
        or len(right) != 1
        or left[0].id == right[0].id
        or left[0].entity_id == right[0].entity_id
    ):
        raise ValueError(
            "Both endpoints require one distinct exact evidence membership on the cited chunk"
        )
    return left[0], right[0]


def _relationship_review_selector(
    result_hint: EntityExtractionResult,
    candidate_id: UUID,
    payload: EntityRelationshipReviewRequest,
) -> tuple[dict[str, Any], str, str, str, UUID]:
    """Select and validate the relationship review snapshot (pure).

    Returns ``(snapshot, source_key, target_key, relationship_type, chunk_id)``. Raises ``LookupError`` when
    the snapshot is absent or not a relationship, ``ValueError`` when the digest changed, a decision exists or
    the endpoint selector is empty/self-referential.
    """
    snapshots = _candidate_snapshots(result_hint.review_json, candidate_id)
    if len(snapshots) != 1 or snapshots[0].get("kind") != "relationship":
        raise LookupError("Relationship review snapshot is unavailable")
    snapshot = snapshots[0]
    if (
        snapshot.get("snapshot_digest") != payload.snapshot_digest
        or _review_snapshot_digest(snapshot) != payload.snapshot_digest
    ):
        raise ValueError("Relationship review snapshot changed")
    if snapshot.get("decision_state"):
        raise ValueError("Relationship review item was already resolved")
    source_key = str(snapshot.get("source_candidate_key", ""))
    target_key = str(snapshot.get("target_candidate_key", ""))
    relationship_type = str(snapshot.get("relationship_type", ""))
    chunk_id = UUID(str(snapshot.get("chunk_id")))
    if not source_key or not target_key or source_key == target_key or not relationship_type:
        raise ValueError("Relationship endpoint selector is invalid")
    return snapshot, source_key, target_key, relationship_type, chunk_id


async def resolve_relationship_review(
    session: AsyncSession,
    candidate_id: UUID,
    payload: EntityRelationshipReviewRequest,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    actor_id: int,
) -> EntityRelationshipReviewResult:
    """Publish a stored relationship only after both exact endpoint memberships exist.

    Lock order: access fence -> Source -> cited evidence -> endpoint entities -> endpoint memberships ->
    extraction work and result rows (FOR UPDATE); the endpoint memberships are re-read after the row locks
    and must be unchanged. Commits once via ``commit_with_replay``.
    """
    from modules.knowledge.documents import public as documents
    from modules.knowledge.relationships import public as relationships

    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    if actor_id != _actor(scope):
        raise ForbiddenError("Review actor does not match the admitted owner")
    result_hint, work_hint = await _review_hints(session, payload.result_id, scope)
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    snapshot, source_key, target_key, relationship_type, chunk_id = _relationship_review_selector(
        result_hint, candidate_id, payload
    )

    document_id, source_id, source = await _lock_review_owner_source(
        session,
        work_hint,
        payload.expected_owner_generation,
        fence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        removed_message="Relationship evidence was removed",
        changed_message="Owner evidence fence changed; reload the relationship review",
    )
    refs = await documents.lock_review_version_evidence(
        session,
        document_id=document_id,
        source_id=source_id,
        version_id=work_hint.document_version_id,
        source_generation=source.generation,
        chunk_ids=[chunk_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if refs is None:
        raise LookupError("Relationship evidence is no longer retained")

    async def endpoint_memberships() -> tuple[EntityEvidenceMembership, EntityEvidenceMembership]:
        """Bind the shared membership lookup to this review item."""
        return await _relationship_endpoint_memberships(
            session,
            scope=scope,
            work_id=work_hint.id,
            document_version_id=work_hint.document_version_id,
            chunk_id=chunk_id,
            source_key=source_key,
            target_key=target_key,
        )

    source_membership, target_membership = await endpoint_memberships()
    expected_membership_ids = (source_membership.id, target_membership.id)
    expected_membership_identity = {
        source_membership.id: (
            source_membership.entity_id,
            source_membership.document_version_id,
            source_membership.chunk_id,
        ),
        target_membership.id: (
            target_membership.entity_id,
            target_membership.document_version_id,
            target_membership.chunk_id,
        ),
    }
    endpoint_refs = await get_entity_refs(
        session,
        [source_membership.entity_id, target_membership.entity_id],
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(endpoint_refs) != 2 or any(
        ref.requested_id != ref.canonical_id for ref in endpoint_refs
    ):
        raise ValueError("Relationship endpoint identity changed; review the assignments")
    locked = await get_membership_refs(
        session,
        [source_membership.id, target_membership.id],
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    work, result = await _lock_review_extraction(session, work_hint.id, payload.result_id, scope)
    if (
        work is None
        or result is None
        or work.status != "succeeded"
        or work.source_generation != extraction_generation
        or work.document_version_id != extraction_version_id
        or work.id != extraction_work_id
    ):
        raise ValueError("Relationship extraction changed; reload the queue")
    current = _candidate_snapshots(result.review_json, candidate_id)
    if (
        len(current) != 1
        or current[0].get("snapshot_digest") != payload.snapshot_digest
        or _review_snapshot_digest(current[0]) != payload.snapshot_digest
        or current[0].get("decision_state")
    ):
        raise ValueError("Relationship review snapshot changed or was resolved")
    source_membership, target_membership = await endpoint_memberships()
    locked_by_id = {
        item.id: (item.entity_id, item.document_version_id, item.chunk_id) for item in locked
    }
    current_membership_ids = (source_membership.id, target_membership.id)
    if (
        current_membership_ids != expected_membership_ids
        or locked_by_id != expected_membership_identity
        or any(item.entity_id not in {ref.canonical_id for ref in endpoint_refs} for item in locked)
    ):
        raise ValueError("Endpoint membership identity changed")
    relationship_id = await relationships.publish_extracted_relationship(
        session,
        source_entity_id=source_membership.entity_id,
        target_entity_id=target_membership.entity_id,
        relationship_type=relationship_type,
        document_version_id=work.document_version_id,
        chunk_id=chunk_id,
        source_membership_id=source_membership.id,
        target_membership_id=target_membership.id,
        confidence=float(snapshot["confidence"]),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if relationship_id is None:
        raise ValueError("Relationship endpoints are identical")
    _set_review_decision(
        result, candidate_id, {"kind": "resolved", "relationship_id": str(relationship_id)}
    )
    await record_owner_action(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_review_resolve",
        reason=payload.reason,
        affected_ids=[relationship_id, candidate_id, source_membership.id, target_membership.id],
    )
    await session.flush()
    await commit_with_replay(
        session,
        [make_graph_change(relationship_id=relationship_id, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return EntityRelationshipReviewResult(
        candidate_id=candidate_id, relationship_id=relationship_id
    )
