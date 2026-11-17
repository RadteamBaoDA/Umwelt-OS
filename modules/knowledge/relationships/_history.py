"""Relationship snapshot history writes and as-of reads.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

import binascii
import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import (
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships._common import (
    MAX_CLEANUP_SUPPORTS,
    _admit,
    _evidence_read,
    _evidence_scope,
    _relationship_read,
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
)
from modules.knowledge.relationships.schemas import (
    EvidenceRead,
    RelationshipPage,
    RelationshipRead,
    RelationshipSnapshot,
)


async def record_relationship_history(
    session: AsyncSession,
    relationship_id: UUID,
    *,
    deleted: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Flush one presently observed owner snapshot; never backdate canonical truth.

    Caller holds canonical mutation fences and commits. Raw citation excerpts,
    titles and URLs are excluded; derived state is physically cleared on purge.
    Complete exact support is bounded and no missing historical state is invented.
    """
    snapshot = await get_relationship_snapshot(
        session, relationship_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if snapshot is None:
        return
    session.add(
        RelationshipSnapshotHistory(
            workspace_id=scope.workspace_id,
            relationship_id=relationship_id,
            deleted=deleted,
            state=snapshot.relationship.model_dump(mode="json", exclude={"evidence"}),
            support=snapshot.supports,
        )
    )
    await session.flush()


async def get_relationship_snapshot(
    session: AsyncSession,
    relationship_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipSnapshot | None:
    """Detach complete current fact/support state under caller-held owner fences.

    Includes all support, endpoint revisions/redirects, exact memberships and
    source generations in deterministic digest. Missing/revoked provenance or
    over10000 support fails closed. Caller holds source/document then entity/fact
    publication locks when comparing for writes; this query never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.id == relationship_id,
        )
    )
    if row is None:
        return None
    supports = list(
        (
            await session.scalars(
                select(RelationshipEvidence)
                .where(
                    _evidence_scope(scope),
                    RelationshipEvidence.relationship_id == relationship_id,
                )
                .order_by(RelationshipEvidence.id)
                .limit(MAX_CLEANUP_SUPPORTS + 1)
            )
        ).all()
    )
    if len(supports) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship snapshot exceeds complete support bound")
    if row.origin == "derived" and not supports:
        raise LookupError("Derived relationship has no permitted support")
    evidence = []
    for offset in range(0, len(supports), 100):
        batch = await _evidence_read(
            session,
            supports[offset : offset + 100],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if len(batch) != len(supports[offset : offset + 100]):
            raise LookupError("Relationship support is unavailable")
        evidence.extend(batch)
    endpoint_refs = await entities.get_entity_refs(
        session,
        [row.source_entity_id, row.target_entity_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    membership_ids = sorted(
        {
            identifier
            for item in supports
            for identifier in (
                item.source_membership_id,
                item.target_membership_id,
            )
            if identifier is not None
        }
    )
    memberships = []
    for offset in range(0, len(membership_ids), 200):
        memberships.extend(
            await entities.get_membership_refs(
                session,
                membership_ids[offset : offset + 200],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    by_id = {item.id: item for item in memberships}
    for item in supports:
        if row.origin == "derived" and (
            item.source_membership_id is None or item.target_membership_id is None
        ):
            raise LookupError("Derived relationship endpoint support is unavailable")
        for identifier, endpoint in (
            (item.source_membership_id, row.source_entity_id),
            (item.target_membership_id, row.target_entity_id),
        ):
            if identifier is not None:
                membership = by_id[identifier]
                if (membership.entity_id, membership.document_version_id, membership.chunk_id) != (
                    endpoint,
                    item.document_version_id,
                    item.chunk_id,
                ):
                    raise LookupError("Relationship endpoint support changed")
    from modules.sources import public as sources

    generations = {}
    for identifier in sorted({item.source_id for item in evidence}):
        source = await sources.get_connector_source(
            session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if source is None or source.status != "active":
            raise LookupError("Relationship source is unavailable")
        generations[str(identifier)] = source.generation
    value = _relationship_read(row, evidence).model_copy(deep=True)
    payload = {
        "relationship": value.model_dump(mode="json"),
        "updated_at": row.updated_at.isoformat(),
        "endpoints": [ref.model_dump(mode="json") for ref in endpoint_refs],
        "memberships": [ref.model_dump(mode="json") for ref in memberships],
        "source_generations": generations,
        "supports": [
            {
                "id": str(item.id),
                "relationship_id": str(item.relationship_id),
                "document_id": str(item.document_id) if item.document_id else None,
                "source_id": str(item.source_id) if item.source_id else None,
                "document_version_id": str(item.document_version_id),
                "chunk_id": str(item.chunk_id),
                "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                "extracted_at": item.extracted_at.isoformat(),
                "confidence": item.confidence,
                "source_membership_id": str(item.source_membership_id)
                if item.source_membership_id
                else None,
                "target_membership_id": str(item.target_membership_id)
                if item.target_membership_id
                else None,
            }
            for item in supports
        ],
    }
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return RelationshipSnapshot(
        relationship=value,
        endpoints=deepcopy(payload["endpoints"]),
        memberships=deepcopy(payload["memberships"]),
        source_generations=generations,
        digest=digest,
        supports=deepcopy(payload["supports"]),
    )


async def _missing_history_ids(
    session: AsyncSession,
    *,
    entity_id: UUID | None,
    knowledge_as_of: datetime,
    scope: Scope,
) -> list[UUID]:
    """Return up to 101 relationships created by the cutoff that have no recorded snapshot at or before it."""
    missing_statement = select(Relationship.id).where(
        Relationship.workspace_id == scope.workspace_id,
        Relationship.created_at <= knowledge_as_of,
        ~select(RelationshipSnapshotHistory.id)
        .where(
            RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
            RelationshipSnapshotHistory.relationship_id == Relationship.id,
            RelationshipSnapshotHistory.recorded_at <= knowledge_as_of,
        )
        .exists(),
    )
    if entity_id is not None:
        missing_statement = missing_statement.where(
            or_(
                Relationship.source_entity_id == entity_id,
                Relationship.target_entity_id == entity_id,
            )
        )
    return list(
        (await session.scalars(missing_statement.order_by(Relationship.id).limit(101))).all()
    )


def _latest_snapshot_statement(
    *, entity_id: UUID | None, knowledge_as_of: datetime, scope: Scope
) -> Any:
    """Select the latest recorded snapshot per relationship at the cutoff, optionally touching ``entity_id``."""
    ranked = (
        select(
            RelationshipSnapshotHistory.id.label("history_id"),
            func.row_number()
            .over(
                partition_by=RelationshipSnapshotHistory.relationship_id,
                order_by=(
                    RelationshipSnapshotHistory.recorded_at.desc(),
                    RelationshipSnapshotHistory.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(
            RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
            RelationshipSnapshotHistory.recorded_at <= knowledge_as_of,
        )
        .subquery()
    )
    statement = (
        select(RelationshipSnapshotHistory)
        .join(
            ranked,
            ranked.c.history_id == RelationshipSnapshotHistory.id,
        )
        .where(
            ranked.c.position == 1, RelationshipSnapshotHistory.workspace_id == scope.workspace_id
        )
    )
    if entity_id is not None:
        statement = statement.where(
            or_(
                RelationshipSnapshotHistory.state["source_entity_id"].astext == str(entity_id),
                RelationshipSnapshotHistory.state["target_entity_id"].astext == str(entity_id),
            )
        )
    return statement


def _decode_bound_cursor(cursor: str, fingerprint: str, message: str) -> tuple[datetime, UUID]:
    """Decode a ``[fingerprint, position]`` keyset cursor (<= 1,024 chars, strict base64) bound to ``fingerprint``.

    Any malformation, fingerprint mismatch (filters changed) or bad position raises ``ValueError(message)``.
    """
    try:
        if len(cursor) > 1024:
            raise ValueError
        bound, position = json.loads(b64url_decode(cursor))
        if bound != fingerprint:
            raise ValueError
        return decode_cursor(position)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError(message) from exc


def _history_evidence_reads(
    history: RelationshipSnapshotHistory,
    item: RelationshipRead,
    by_pair: dict[tuple[UUID, UUID], Any],
    by_membership: dict[UUID, Any],
    canonical_endpoints: dict[UUID, UUID],
) -> list[EvidenceRead]:
    """Project retained support rows onto currently readable evidence refs.

    Pure over already-read refs: skips support whose ref is no longer permitted, and raises
    ``LookupError`` when a derived edge lacks endpoint membership or a membership no longer matches
    the canonical endpoint entity or the cited document version/chunk.
    """
    evidence = []
    for support in history.support:
        pair = UUID(str(support["document_version_id"])), UUID(str(support["chunk_id"]))
        ref = by_pair.get(pair)
        if ref is None:
            continue
        for key, endpoint in (
            ("source_membership_id", item.source_entity_id),
            ("target_membership_id", item.target_entity_id),
        ):
            if not support.get(key):
                if item.origin == "derived":
                    raise LookupError("Historical endpoint support unavailable")
                continue
            member = by_membership[UUID(str(support[key]))]
            if (
                member.entity_id != canonical_endpoints[endpoint]
                or (member.document_version_id, member.chunk_id) != pair
            ):
                raise LookupError("Historical endpoint support changed")
        evidence.append(
            EvidenceRead(
                id=UUID(str(support["id"])),
                relationship_id=item.id,
                document_id=ref.document_id,
                document_version_id=ref.document_version_id,
                version_number=ref.version_number,
                chunk_id=ref.chunk_id,
                observed_at=ref.observed_at,
                extracted_at=datetime.fromisoformat(str(support["extracted_at"])),
                confidence=float(support["confidence"]),
                source_entity_membership_id=UUID(str(support["source_membership_id"]))
                if support.get("source_membership_id")
                else None,
                target_entity_membership_id=UUID(str(support["target_membership_id"]))
                if support.get("target_membership_id")
                else None,
                title=ref.title,
                canonical_url=ref.canonical_url,
                source_id=ref.source_id,
                excerpt=ref.excerpt,
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
            )
        )
    return evidence


async def _read_history_memberships(
    session: AsyncSession,
    history: RelationshipSnapshotHistory,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Read the entity memberships cited by a snapshot's support rows, 200 ids per call.

    Read-only and ordered by sorted membership id so batches are deterministic.
    """
    membership_ids = sorted(
        {
            UUID(str(s[key]))
            for s in history.support
            for key in ("source_membership_id", "target_membership_id")
            if s.get(key)
        }
    )
    memberships = []
    for offset in range(0, len(membership_ids), 200):
        memberships.extend(
            await entities.get_membership_refs(
                session,
                membership_ids[offset : offset + 200],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    return memberships


async def _hydrate_history_item(
    session: AsyncSession,
    history: RelationshipSnapshotHistory,
    item: RelationshipRead,
    *,
    knowledge_as_of: datetime,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipRead | None:
    """Re-check retained support for one historical snapshot and attach its evidence.

    Re-reads endpoints, evidence refs, active sources and memberships under current permissions. Returns
    ``None`` when a derived relationship lost any support (the caller reports it unavailable); raises
    ``LookupError``/``ValueError`` when endpoint support changed or exceeds its bound (also unavailable).
    """
    from modules.sources import public as sources

    endpoint_refs = await entities.get_entity_refs(
        session,
        [item.source_entity_id, item.target_entity_id],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    canonical_endpoints = {ref.requested_id: ref.canonical_id for ref in endpoint_refs}
    pairs = list(
        dict.fromkeys(
            (UUID(str(s["document_version_id"])), UUID(str(s["chunk_id"]))) for s in history.support
        )
    )
    if len(pairs) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Historical snapshot support exceeds its bound")
    refs = []
    for offset in range(0, len(pairs), 100):
        refs.extend(
            await documents.read_evidence_refs(
                session,
                pairs[offset : offset + 100],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    permitted_sources = set()
    for source_id in sorted({ref.source_id for ref in refs}):
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if source is not None and source.status == "active":
            permitted_sources.add(source_id)
    by_pair = {
        (ref.document_version_id, ref.chunk_id): ref
        for ref in refs
        if ref.source_id in permitted_sources and ref.observed_at <= knowledge_as_of
    }
    # Any missing support can have contributed to the retained derived fields.
    if item.origin == "derived" and (not pairs or len(by_pair) != len(pairs)):
        return None
    memberships = await _read_history_memberships(
        session, history, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    by_membership = {member.id: member for member in memberships}
    evidence = _history_evidence_reads(history, item, by_pair, by_membership, canonical_endpoints)
    item.evidence = evidence
    return item


async def _read_history_snapshot_page(
    session: AsyncSession,
    statement: Any,
    *,
    cursor: str | None,
    fingerprint: str,
    limit: int,
) -> tuple[list[RelationshipSnapshotHistory], str | None]:
    """Read one page of latest snapshot rows after ``cursor`` and build the next bound cursor.

    Fetches ``limit + 1`` rows ordered newest first; an invalid or foreign cursor raises through
    ``_decode_bound_cursor``. Returns the page rows and the next cursor (``None`` on the last page).
    """
    if cursor:
        recorded_at, identifier = _decode_bound_cursor(
            cursor, fingerprint, "Invalid relationship history cursor"
        )
        statement = statement.where(
            tuple_(
                RelationshipSnapshotHistory.recorded_at, RelationshipSnapshotHistory.relationship_id
            )
            < (recorded_at, identifier)
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(
                    RelationshipSnapshotHistory.recorded_at.desc(),
                    RelationshipSnapshotHistory.relationship_id.desc(),
                ).limit(limit + 1)
            )
        ).all()
    )
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = b64url_encode(
            json.dumps(
                [
                    fingerprint,
                    encode_cursor(rows[-1].recorded_at, rows[-1].relationship_id),
                ]
            ).encode()
        )
    return rows, next_cursor


async def _list_relationships_as_of(
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    entity_id: UUID | None,
    valid_at: datetime | None,
    include_unknown_validity: bool,
    knowledge_as_of: datetime,
    fingerprint: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipPage:
    """Page actual latest recorded canonical snapshots by transaction cutoff.

    Current deletion/permission and retained exact support are checked again.
    Never return current fields as historical substitutes; unavailable intervals
    are explicit. Pagination covers snapshot identities, not loaded current rows.
    Query order: missing-snapshot probe, then (after cursor validation) the snapshot page, one batched
    existence probe for the page, then per-item support re-reads. Read-only; no commit.
    """
    missing = await _missing_history_ids(
        session, entity_id=entity_id, knowledge_as_of=knowledge_as_of, scope=scope
    )
    statement = _latest_snapshot_statement(
        entity_id=entity_id, knowledge_as_of=knowledge_as_of, scope=scope
    )
    rows, next_cursor = await _read_history_snapshot_page(
        session, statement, cursor=cursor, fingerprint=fingerprint, limit=limit
    )
    # One existence probe for the whole page (<= limit ids) instead of one query per snapshot row.
    candidate_ids = [h.relationship_id for h in rows if not h.deleted and h.state]
    live_ids = (
        set(
            (
                await session.scalars(
                    select(Relationship.id).where(
                        Relationship.workspace_id == scope.workspace_id,
                        Relationship.id.in_(candidate_ids),
                    )
                )
            ).all()
        )
        if candidate_ids
        else set()
    )
    items, unavailable = [], []
    for history in rows:
        if history.deleted:
            continue
        if not history.state or history.relationship_id not in live_ids:
            unavailable.append(history.relationship_id)
            continue
        item = RelationshipRead.model_validate(history.state)
        unknown = item.valid_from is None or item.valid_to is None
        if unknown and not include_unknown_validity:
            continue
        if valid_at is not None and (
            (item.valid_from is not None and item.valid_from > valid_at)
            or (item.valid_to is not None and item.valid_to <= valid_at)
        ):
            continue
        try:
            hydrated = await _hydrate_history_item(
                session,
                history,
                item,
                knowledge_as_of=knowledge_as_of,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except (LookupError, ValueError):
            unavailable.append(history.relationship_id)
            continue
        if hydrated is None:
            unavailable.append(history.relationship_id)
            continue
        items.append(hydrated)
    return RelationshipPage(
        items=items,
        next_cursor=next_cursor,
        knowledge_as_of=knowledge_as_of,
        canonical_history_available=not missing and not unavailable,
        observation_history_only=False,
        unavailable_relationship_ids=list(dict.fromkeys(unavailable + missing[:100])),
    )
