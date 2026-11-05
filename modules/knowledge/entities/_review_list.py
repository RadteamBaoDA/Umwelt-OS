"""Entity review-candidate listing, cursors and snapshot digests.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_encode
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities._reads import get_entity_refs
from modules.knowledge.entities.models import (
    EntityEvidenceMembership,
    EntityExtractionResult,
    EntityExtractionWork,
)
from modules.knowledge.entities.schemas import (
    EntityReviewCandidate,
    EntityReviewEndpoint,
    EntityReviewEvidence,
    EntityReviewPage,
)


def _review_cursor(timestamp: datetime, work_id: UUID, index: int) -> str:
    """Encode a review item position as unpadded URL-safe JSON cursor data."""
    raw = json.dumps([timestamp.isoformat(), str(work_id), index], separators=(",", ":")).encode()
    return b64url_encode(raw)


def _decode_review_cursor(value: str) -> tuple[datetime, UUID, int]:
    """Validate and decode a review cursor, rejecting malformed positions."""
    try:
        if not value or len(value) > 512:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
        if (
            not isinstance(decoded, list)
            or len(decoded) != 3
            or not isinstance(decoded[0], str)
            or not isinstance(decoded[1], str)
            or type(decoded[2]) is not int
        ):
            raise ValueError
        timestamp, work_id, index = decoded
        result = datetime.fromisoformat(timestamp), UUID(work_id), index
        if result[2] < 0 or result[2] > 100:
            raise ValueError
        return result
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid review cursor") from exc


def _review_candidate_item(
    work: EntityExtractionWork,
    result: EntityExtractionResult,
    candidate: dict[str, Any],
    state: object,
    fences: dict[UUID, Any],
) -> (
    tuple[EntityReviewCandidate, list[tuple[UUID, UUID]], tuple[UUID, UUID, str, str, UUID] | None]
    | None
):
    """Project one stored review candidate (pure) into its DTO plus evidence and endpoint lookup keys.

    Returns ``None`` for malformed candidates (non-string name/reason). The item is actionable only for a
    succeeded work row with an immutable matching digest, a live version fence and the identity fields of its
    kind; malformed ids/chunk ids are silently skipped, evidence is capped at 5 chunks per entity candidate.
    """
    name, reason = (
        candidate.get("candidate_name", candidate.get("candidate_name")),
        candidate.get("reason"),
    )
    if not isinstance(name, str) or not isinstance(reason, str):
        return None
    ids = candidate.get("possible_entity_ids", [])
    valid_ids = []
    for value in ids if isinstance(ids, list) else []:
        try:
            valid_ids.append(UUID(str(value)))
        except ValueError:
            continue
    kind = (
        candidate.get("kind")
        if candidate.get("kind") in {"entity", "relationship"}
        else ("relationship" if reason.startswith("relationship_") else "entity")
    )
    candidate_id = None
    try:
        candidate_id = (
            UUID(str(candidate.get("candidate_id"))) if candidate.get("candidate_id") else None
        )
    except ValueError:
        pass
    digest_ok = False
    try:
        digest_ok = candidate.get("snapshot_digest") == _review_snapshot_digest(candidate)
    except (KeyError, TypeError):
        pass
    actionable = bool(
        candidate_id
        and digest_ok
        and not state
        and work.status == "succeeded"
        and (
            work.document_version_id in fences
            and (
                kind == "entity"
                and candidate.get("candidate_key")
                and candidate.get("match_fingerprint")
                and candidate.get("chunk_ids")
                or kind == "relationship"
                and candidate.get("source_candidate_key")
                and candidate.get("target_candidate_key")
                and candidate.get("chunk_id")
                and candidate.get("relationship_type")
            )
        )
    )
    candidate_evidence: list[tuple[UUID, UUID]] = []
    if kind == "entity" and isinstance(candidate.get("chunk_ids"), list):
        for raw_chunk in candidate["chunk_ids"][:5]:
            try:
                candidate_evidence.append((work.document_version_id, UUID(str(raw_chunk))))
            except (ValueError, TypeError):
                continue
    elif kind == "relationship" and candidate.get("chunk_id"):
        try:
            candidate_evidence.append((work.document_version_id, UUID(str(candidate["chunk_id"]))))
        except (ValueError, TypeError):
            pass
    fence = fences.get(work.document_version_id)
    item = EntityReviewCandidate(
        kind=kind,
        candidate_id=candidate_id,
        work_id=work.id,
        result_id=result.id,
        snapshot_digest=candidate.get("snapshot_digest") if actionable else None,
        document_version_id=work.document_version_id,
        source_generation=work.source_generation,
        owner_generation=fence.current_source_generation if fence else None,
        document_id=fence.document_id if fence else None,
        version_number=fence.version_number if fence else None,
        source_id=fence.source_id if fence else None,
        source_name=fence.source_name if fence else None,
        actionable=actionable,
        status=work.status,
        candidate_name=name[:300],
        candidate_type=str(candidate.get("candidate_type"))
        if candidate.get("candidate_type")
        else None,
        reason=reason[:128],
        possible_entity_ids=valid_ids[:30],
        relationship_type=str(candidate.get("relationship_type"))
        if kind == "relationship" and candidate.get("relationship_type")
        else None,
    )
    endpoint_key = (
        (
            work.id,
            work.document_version_id,
            str(candidate.get("source_candidate_key")),
            str(candidate.get("target_candidate_key")),
            candidate_evidence[0][1],
        )
        if kind == "relationship"
        and candidate.get("source_candidate_key")
        and candidate.get("target_candidate_key")
        and candidate_evidence
        else None
    )
    return item, candidate_evidence, endpoint_key


def _collect_review_items(
    rows: list[Any],
    fences: dict[UUID, Any],
    limit: int,
    cursor_work: UUID | None,
    cursor_index: int,
) -> tuple[
    list[EntityReviewCandidate],
    list[list[tuple[UUID, UUID]]],
    list[tuple[UUID, UUID, str, str, UUID] | None],
    str | None,
]:
    """Walk up to 100 work rows and collect at most ``limit`` undecided review items (pure).

    Resumes inside the cursor's work row at ``cursor_index``, skips candidates with a terminal decision state,
    and computes the continuation cursor: next candidate of the same row, the row end, or row 100 when more
    than 100 rows were fetched.
    """
    items: list[EntityReviewCandidate] = []
    evidence_keys: list[list[tuple[UUID, UUID]]] = []
    endpoint_keys: list[tuple[UUID, UUID, str, str, UUID] | None] = []
    next_cursor = None
    for row_index, (work, result) in enumerate(rows[:100]):
        review = result.review_json if isinstance(result.review_json, list) else []
        start = cursor_index if cursor_work == work.id else 0
        cursor_work = None
        for candidate_index, candidate in enumerate(review[start:], start=start):
            if not isinstance(candidate, dict):
                continue
            state = candidate.get("decision_state")
            if isinstance(state, dict) and state.get("kind") in {
                "assigned",
                "resolved",
                "suppressed",
            }:
                continue
            projected = _review_candidate_item(work, result, candidate, state, fences)
            if projected is None:
                continue
            item, candidate_evidence, endpoint_key = projected
            items.append(item)
            evidence_keys.append(candidate_evidence)
            endpoint_keys.append(endpoint_key)
            if len(items) == limit:
                if candidate_index + 1 < len(review):
                    next_cursor = _review_cursor(work.updated_at, work.id, candidate_index + 1)
                elif row_index + 1 < 100 or len(rows) > 100:
                    next_cursor = _review_cursor(work.updated_at, work.id, len(review))
                break
        if len(items) == limit:
            break
    if len(items) < limit and len(rows) > 100:
        work, result = rows[99]
        review = result.review_json if isinstance(result.review_json, list) else []
        next_cursor = _review_cursor(work.updated_at, work.id, len(review))
    return items, evidence_keys, endpoint_keys, next_cursor


def _review_endpoint_dtos(
    endpoints: tuple[UUID, UUID, str, str, UUID],
    endpoint_rows: list[Any],
    entity_refs_by_id: dict[UUID, Any],
) -> list[EntityReviewEndpoint]:
    """Resolve a relationship candidate's source/target endpoint memberships to assigned/ambiguous/unassigned DTOs."""
    work_id, version_id, source_key, target_key, chunk_id = endpoints
    endpoint_dtos = []
    for key in (source_key, target_key):
        matches = [
            row
            for row in endpoint_rows
            if row.extraction_identity == str(work_id)
            and row.candidate_key == key
            and row.document_version_id == version_id
            and row.chunk_id == chunk_id
        ]
        if len(matches) != 1:
            endpoint_dtos.append(
                EntityReviewEndpoint(state="ambiguous" if matches else "unassigned")
            )
            continue
        member = matches[0]
        endpoint_ref = entity_refs_by_id.get(member.entity_id)
        if endpoint_ref is None:
            endpoint_dtos.append(EntityReviewEndpoint(state="unassigned"))
            continue
        endpoint_dtos.append(
            EntityReviewEndpoint(
                state="assigned",
                entity_id=endpoint_ref.canonical_id,
                entity_name=endpoint_ref.name,
                entity_type=endpoint_ref.type,
                membership_id=member.id,
            )
        )
    return endpoint_dtos


async def _attach_review_evidence(
    session: AsyncSession,
    items: list[EntityReviewCandidate],
    evidence_keys: list[list[tuple[UUID, UUID]]],
    endpoint_keys: list[tuple[UUID, UUID, str, str, UUID] | None],
    fences: dict[UUID, Any],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Read evidence refs (100 per call) and relationship endpoint memberships, then fill each item in place.

    Relationship items whose endpoints are not two distinct assigned entities lose their actionability and
    digest. Read-only; evidence reads go through the documents owner, entity refs through ``get_entity_refs``.
    """
    from modules.knowledge.documents import public as documents

    all_keys = list(dict.fromkeys(key for group in evidence_keys for key in group))
    refs_by_pair = {}
    for offset in range(0, len(all_keys), 100):
        refs = await documents.read_evidence_refs(
            session,
            all_keys[offset : offset + 100],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        refs_by_pair.update({(ref.document_version_id, ref.chunk_id): ref for ref in refs})
    endpoint_bindings = list(
        dict.fromkeys(
            (str(work_id), key, version_id, chunk_id)
            for endpoints in endpoint_keys
            if endpoints is not None
            for work_id, version_id, source_key, target_key, chunk_id in (endpoints,)
            for key in (source_key, target_key)
        )
    )
    endpoint_rows = (
        list(
            (
                await session.scalars(
                    select(EntityEvidenceMembership)
                    .where(
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        tuple_(
                            EntityEvidenceMembership.extraction_identity,
                            EntityEvidenceMembership.candidate_key,
                            EntityEvidenceMembership.document_version_id,
                            EntityEvidenceMembership.chunk_id,
                        ).in_(endpoint_bindings),
                    )
                    .order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id)
                    .limit(401)
                )
            ).all()
        )
        if endpoint_bindings
        else []
    )
    endpoint_entities = sorted({row.entity_id for row in endpoint_rows}, key=str)
    entity_refs = []
    for offset in range(0, len(endpoint_entities), 100):
        entity_refs.extend(
            await get_entity_refs(
                session,
                endpoint_entities[offset : offset + 100],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    entity_refs_by_id = {ref.requested_id: ref for ref in entity_refs}
    for item, keys, endpoints in zip(items, evidence_keys, endpoint_keys, strict=False):
        fence = fences.get(item.document_version_id)
        item.evidence = [
            EntityReviewEvidence(
                document_id=ref.document_id,
                document_version_id=ref.document_version_id,
                version_number=ref.version_number,
                chunk_id=ref.chunk_id,
                source_id=ref.source_id,
                source_name=fence.source_name if fence else "",
                title=ref.title,
                canonical_url=ref.canonical_url,
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
                observed_at=ref.observed_at,
                excerpt=ref.excerpt,
            )
            for key in keys
            if (ref := refs_by_pair.get(key)) is not None
        ]
        if endpoints is None:
            continue
        endpoint_dtos = _review_endpoint_dtos(endpoints, endpoint_rows, entity_refs_by_id)
        item.source_endpoint, item.target_endpoint = endpoint_dtos
        if (
            len(endpoint_dtos) != 2
            or any(endpoint.state != "assigned" for endpoint in endpoint_dtos)
            or endpoint_dtos[0].entity_id == endpoint_dtos[1].entity_id
        ):
            item.actionable = False
            item.snapshot_digest = None


async def list_review_candidates(
    session: AsyncSession,
    limit: int = 50,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityReviewPage:
    """Bounded owner projection; only immutable future snapshots are actionable."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Review page limit must be between 1 and 100")
    cursor_time, cursor_work, cursor_index = (
        _decode_review_cursor(cursor) if cursor else (None, None, 0)
    )
    statement = (
        select(EntityExtractionWork, EntityExtractionResult)
        .join(EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id)
        .where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionResult.review_json.is_not(None),
        )
    )
    if cursor_time is not None and cursor_work is not None:
        statement = statement.where(
            tuple_(EntityExtractionWork.updated_at, EntityExtractionWork.id)
            <= (cursor_time, cursor_work)
        )
    rows = (
        await session.execute(
            statement.order_by(
                EntityExtractionWork.updated_at.desc(), EntityExtractionWork.id.desc()
            ).limit(101)
        )
    ).all()
    from modules.knowledge.documents import public as documents

    fences = await documents.review_version_fences(
        session,
        [work.document_version_id for work, _ in rows[:100]],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    items, evidence_keys, endpoint_keys, next_cursor = _collect_review_items(
        list(rows),
        fences,
        limit,
        cursor_work,
        cursor_index,
    )
    await _attach_review_evidence(
        session,
        items,
        evidence_keys,
        endpoint_keys,
        fences,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return EntityReviewPage(items=items, next_cursor=next_cursor)


def _review_snapshot_digest(candidate: dict[str, object]) -> str:
    """Hash the immutable identity fields used to fence owner review actions."""
    keys = (
        (
            "kind",
            "candidate_id",
            "candidate_type",
            "candidate_name",
            "candidate_key",
            "match_fingerprint",
            "chunk_ids",
            "confidence",
        )
        if candidate.get("kind") == "entity"
        else (
            "kind",
            "candidate_id",
            "candidate_name",
            "relationship_type",
            "source_candidate_key",
            "target_candidate_key",
            "chunk_id",
            "confidence",
        )
    )
    snapshot = {key: candidate[key] for key in keys}
    return sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _review_entity_bindings(review: list[Any]) -> dict[str, tuple[str, set[UUID]]]:
    """Validate entity review selectors and return bounded fingerprint/chunk bindings."""
    bindings: dict[str, tuple[str, set[UUID]]] = {}
    for item in review:
        if not isinstance(item, dict) or item.get("kind") != "entity":
            continue
        fingerprint, raw_chunks = item.get("match_fingerprint"), item.get("chunk_ids")
        if fingerprint is None and raw_chunks is None:
            continue
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
            or not isinstance(raw_chunks, list)
            or not 1 <= len(raw_chunks) <= 5
            or not isinstance(item.get("candidate_key"), str)
            or len(item["candidate_key"]) != 64
            or not item.get("candidate_id")
        ):
            raise ValueError("Same-result candidate selectors are incomplete; reload the review")
        try:
            chunks = {UUID(str(value)) for value in raw_chunks}
            key = str(UUID(str(item["candidate_id"])))
            if item.get("snapshot_digest") != _review_snapshot_digest(item):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(
                "Same-result candidate selectors are invalid; reload the review"
            ) from exc
        if len(chunks) != len(raw_chunks) or key in bindings:
            raise ValueError("Same-result candidate selectors are ambiguous; reload the review")
        bindings[key] = (fingerprint, chunks)
    if len(bindings) > 30:
        raise ValueError("Same-result candidate selector set exceeds its limit")
    return bindings


async def _review_hints(
    session: AsyncSession,
    result_id: UUID,
    scope: Scope,
) -> tuple[EntityExtractionResult, EntityExtractionWork]:
    """Read (unlocked) the workspace-scoped extraction result and its work row for a review action.

    Raises ``LookupError`` when either row is unavailable. The rows are hints only; the caller re-reads and
    locks them FOR UPDATE after the owner Source and evidence locks.
    """
    result_hint = await session.scalar(
        select(EntityExtractionResult)
        .join(
            EntityExtractionWork,
            EntityExtractionWork.id == EntityExtractionResult.work_id,
        )
        .where(
            EntityExtractionResult.id == result_id,
            EntityExtractionWork.workspace_id == scope.workspace_id,
        )
    )
    if result_hint is None:
        raise LookupError("Review result is unavailable")
    work_hint = await session.scalar(
        select(EntityExtractionWork).where(
            EntityExtractionWork.id == result_hint.work_id,
            EntityExtractionWork.workspace_id == scope.workspace_id,
        )
    )
    if work_hint is None:
        raise LookupError("Review work is unavailable")
    return result_hint, work_hint
