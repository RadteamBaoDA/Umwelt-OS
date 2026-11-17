"""Publication of resolved entities and relationships and the ``process_entity_extraction_work`` job."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import error_status
from core.ids import uuid7
from core.job_denial import (
    PERMISSION_LOST,
    terminalize,
)
from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.realtime import commit_with_replay, make_graph_change
from core.worker_context import worker_session_factory as _factory
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._worker.claim import (
    _call_provider,
    _candidate_key,
    _capture_extraction_snapshot,
    _Claim,
    _claim,
    _Plan,
    _Published,
    _relock_and_compare,
    _RunState,
    _Skip,
    _Snapshot,
    _validate_extraction_response,
    _with_digest,
)
from modules.knowledge.entities._worker.plan import (
    _entity_review_item,
    _read_evidence_by_chunk,
    _resolve_entities,
)
from modules.knowledge.entities.extraction import (
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionOutput,
)
from modules.knowledge.entities.models import EntityExtractionWork
from modules.knowledge.relationships import public as relationships

logger = logging.getLogger("modules.knowledge.entities.worker")  # same logger object as the facade


async def _publish_entity(
    session: AsyncSession,
    snap: _Snapshot,
    candidate: ExtractedEntity,
    plan_entry: _Plan,
    fingerprint: str,
    evidence_by_chunk: dict[UUID, Any],
    published: _Published,
) -> None:
    """Create or reuse the entity for one resolved candidate and record its per-chunk memberships.

    A new entity also gets derived ``name`` (and ``description``) fields for each membership. Mutates
    ``published``; writes only inside the caller's transaction.
    """
    claim = snap.claim
    _resolution, match_id, _possible, _reason = plan_entry
    new_entity = plan_entry[0] == "new"
    entity_id = (
        UUID(match_id)
        if match_id
        else await entities.create_extracted_entity(
            session,
            candidate.type,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
    )
    published.key_to_entity[candidate.key] = entity_id
    candidate_key = _candidate_key(candidate.type, candidate.key)
    for chunk_id in candidate.chunk_ids:
        membership_id = await entities.record_extraction_membership(
            session,
            entity_id=entity_id,
            evidence_ref=evidence_by_chunk[chunk_id],
            source_generation=claim.captured_generation,
            extraction_identity=str(claim.work_id),
            candidate_key=candidate_key,
            match_fingerprint=fingerprint,
            observed_at=snap.data.observed_at,
            confidence=candidate.confidence,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        published.membership_for[(candidate.key, chunk_id)] = membership_id
        if new_entity:
            await entities.publish_derived_field(
                session,
                entity_id=entity_id,
                membership_id=membership_id,
                field_name="name",
                value=candidate.name,
                scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
            if candidate.description:
                await entities.publish_derived_field(
                    session,
                    entity_id=entity_id,
                    membership_id=membership_id,
                    field_name="description",
                    value=candidate.description,
                    scope=claim.scope,
                    multi_workspace_enabled=claim.multi_workspace_enabled,
                )
    published.facts.append(
        {
            "entity_id": str(entity_id),
            "candidate_key": candidate.key,
            "confidence": candidate.confidence,
        }
    )


async def _publish_entities(
    session: AsyncSession,
    snap: _Snapshot,
    extracted: ExtractionOutput,
    plan: dict[str, _Plan],
    match_fingerprints: dict[str, str],
    evidence_by_chunk: dict[UUID, Any],
) -> _Published:
    """Publish resolved candidates in response order; review candidates are only snapshotted."""
    published = _Published(key_to_entity={}, membership_for={}, review=[], facts=[])
    for candidate in extracted.entities:
        entry = plan[candidate.key]
        if entry[0] == "review":
            published.review.append(
                _entity_review_item(
                    candidate,
                    match_fingerprints[candidate.key],
                    entry[3],
                    entry[2],
                )
            )
            continue
        await _publish_entity(
            session,
            snap,
            candidate,
            entry,
            match_fingerprints[candidate.key],
            evidence_by_chunk,
            published,
        )
    return published


def _retain_relationship_review(
    published: _Published,
    relation: ExtractedRelationship,
    candidate_by_key: dict[str, ExtractedEntity],
    reason: str,
    possible: list[str],
) -> None:
    """Snapshot an unresolved relationship's endpoints for later owner review (appends to ``published.review``)."""
    source_candidate = candidate_by_key[relation.source_key]
    target_candidate = candidate_by_key[relation.target_key]
    snapshot: dict[str, object] = {
        "kind": "relationship",
        "candidate_id": str(uuid7()),
        "candidate_name": relation.type,
        "relationship_type": relation.type,
        "source_candidate_key": _candidate_key(source_candidate.type, source_candidate.key),
        "target_candidate_key": _candidate_key(target_candidate.type, target_candidate.key),
        "chunk_id": str(relation.chunk_id),
        "confidence": relation.confidence,
    }
    _with_digest(snapshot)
    published.review.append({**snapshot, "reason": reason, "possible_entity_ids": possible})


async def _publish_relationships(
    session: AsyncSession,
    snap: _Snapshot,
    extracted: ExtractionOutput,
    published: _Published,
) -> None:
    """Publish edges whose endpoints both resolved to entities with same-chunk evidence; retain the rest for review."""
    claim = snap.claim
    candidate_by_key = {candidate.key: candidate for candidate in extracted.entities}
    for relation in extracted.relationships:
        source_id = published.key_to_entity.get(relation.source_key)
        target_id = published.key_to_entity.get(relation.target_key)
        if source_id is None or target_id is None:
            _retain_relationship_review(
                published, relation, candidate_by_key, "relationship_endpoint_requires_review", []
            )
            continue
        source_membership = published.membership_for.get((relation.source_key, relation.chunk_id))
        target_membership = published.membership_for.get((relation.target_key, relation.chunk_id))
        if source_membership is None or target_membership is None or source_id == target_id:
            _retain_relationship_review(
                published,
                relation,
                candidate_by_key,
                "relationship_identity_ambiguous",
                [str(source_id), str(target_id)],
            )
            continue
        relationship_id = await relationships.publish_extracted_relationship(
            session,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relationship_type=relation.type,
            document_version_id=claim.version_id,
            chunk_id=relation.chunk_id,
            source_membership_id=source_membership,
            target_membership_id=target_membership,
            confidence=relation.confidence,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        if relationship_id is not None:
            published.facts.append(
                {
                    "relationship_id": str(relationship_id),
                    "relationship": relation.type,
                    "source_entity_id": str(source_id),
                    "target_entity_id": str(target_id),
                    "chunk_id": str(relation.chunk_id),
                    "confidence": relation.confidence,
                }
            )


async def _publish_extraction(session: AsyncSession, snap: _Snapshot, response: object) -> None:
    """Phase 3: validate, re-lock, resolve and publish the graph, then acknowledge the lease.

    One transaction on ``session``: pure checks run before any lock is reacquired, ``finish_extraction_work``
    is the single ack, and ``commit_with_replay`` is the only commit. If the lease was lost the whole
    transaction is rolled back.
    """
    claim = snap.claim
    extracted, actual_model, usage = _validate_extraction_response(response, snap.data)
    await _relock_and_compare(session, snap)
    evidence_by_chunk = await _read_evidence_by_chunk(session, snap, extracted)
    plan, match_fingerprints = await _resolve_entities(session, snap, extracted)
    published = await _publish_entities(
        session, snap, extracted, plan, match_fingerprints, evidence_by_chunk
    )
    await _publish_relationships(session, snap, extracted, published)
    mapping = snap.mapping
    if mapping is None:
        raise RuntimeError("invariant violated: mapping must not be None")
    if await entities.finish_extraction_work(
        session,
        claim.work_id,
        claim.lease_owner,
        facts=published.facts,
        review=published.review,
        model=actual_model or mapping.model,
        usage=usage,
        scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    ):
        await commit_with_replay(
            session,
            [
                make_graph_change(entity_id=entity_id, scope=claim.scope)
                for entity_id in sorted(set(published.key_to_entity.values()), key=str)
            ],
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
            access_fence=snap.access_fence,
        )
    else:
        await session.rollback()


async def _record_failure(
    factory: async_sessionmaker[AsyncSession],
    claim: _Claim,
    error_code: str,
    *,
    blocked: bool,
    dependency_fingerprint: str | None = None,
) -> None:
    """Record ``error_code`` against the lease-owned work on a fresh session and commit.

    Used by every failure arm because the failed transaction's session is unusable; the update is a no-op
    when this worker no longer owns the lease.
    """
    async with factory() as session:
        await entities.set_extraction_work_error(
            session,
            claim.work_id,
            claim.lease_owner,
            error_code,
            blocked=blocked,
            dependency_fingerprint=dependency_fingerprint,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        await session.commit()


async def process_entity_extraction_work(ctx: dict[str, object], work_id_value: str) -> None:
    """Run one lease-owned extraction job and publish only fenced graph changes.

    Commits the work claim (lease = background LLM timeout + 75 s, at least 110 s), locks source/document only to capture plain
    DTOs, then releases every lock before the provider request so chat's access
    fence is never blocked. Privacy policy and the access fence are rechecked on
    an independent session immediately before send; after the response the worker
    re-locks and compares source generation, raw URI and MIME type before
    publication (drift raises retryable ``document_unavailable``).
    Policy/capability blocks with a saved dependency fingerprint can be requeued
    after recovery detects a changed dependency. Local-only blocks have no
    fingerprint and remain parked at ``datetime.max``; bounded-input blocks are
    also blocked without dependency requeue. Transport and general failures use
    the bounded work retry policy. Graph writes roll back if final lease
    acknowledgement no longer belongs to this worker.
    """
    factory = _factory(ctx)
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    work_id = UUID(work_id_value)
    lease_owner = hashlib.sha256(f"{work_id}:{datetime.now(UTC).isoformat()}".encode()).hexdigest()[
        :48
    ]
    state = _RunState()
    async with factory() as session:
        claim = await _claim(session, factory, settings, work_id, lease_owner)
    if claim is None:
        return
    try:
        async with factory() as session:
            snap = await _capture_extraction_snapshot(session, claim, settings, redis, state)
            if isinstance(snap, _Skip):
                return
            response = await _call_provider(session, factory, settings, redis, snap)
            await _publish_extraction(session, snap, response)
    except (PrivacyPolicyDenied, CapabilityUnsupported) as exc:
        await _record_failure(
            factory,
            claim,
            "ai_policy_denied"
            if isinstance(exc, PrivacyPolicyDenied)
            else "structured_unsupported",
            blocked=True,
            dependency_fingerprint=state.dependency_fingerprint,
        )
    except ModelGatewayError:
        blocked = False  # no evidence gate any more: only transport/provider errors reach here, all retryable
        error_code = "model_transport_unavailable"
        await _record_failure(
            factory,
            claim,
            error_code,
            blocked=blocked,
            dependency_fingerprint=state.dependency_fingerprint if blocked else None,
        )
        # A blocked capability is a configuration state that repeats for every queued item until the model
        # settings change, so it stays out of the warning stream; transport failures are still warnings.
        (logger.debug if blocked else logger.warning)(
            "Entity extraction deferred work_id=%s error_code=%s", work_id, error_code
        )
    except Exception as exc:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        status = error_status(exc)
        if status is not None and status != 409:
            # Lost permission mid-run is terminal; a 409 keeps the attempt-budgeted retry (re-resolved at next claim).
            await terminalize(
                factory, EntityExtractionWork, work_id, claim.scope.workspace_id, PERMISSION_LOST
            )
            return
        blocked = str(exc) in {"Extraction input exceeds its chunk or byte limit"}
        error_code = (
            "extraction_input_limit"
            if blocked
            else str(exc)
            if str(exc)
            in {
                "invalid_model_response",
                "unknown_evidence_chunk",
                "duplicate_candidate_key",
                "invalid_relationship_endpoint",
                "document_unavailable",
            }
            else "extraction_failed"
        )
        await _record_failure(factory, claim, error_code, blocked=blocked)
        logger.warning("Entity extraction failed work_id=%s error_code=%s", work_id, error_code)
