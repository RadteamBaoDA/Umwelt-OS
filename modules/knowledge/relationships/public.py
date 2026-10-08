import base64
import binascii
import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    ColumnElement,
    Select,
    delete,
    desc,
    exists,
    false,
    func,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_graph_change
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
)
from modules.knowledge.relationships.schemas import (
    CorrectionRelationshipRef,
    CorrectionSupportRef,
    EntityGraphRead,
    EvidenceRead,
    NeighborPage,
    NeighborRead,
    RelationshipCreate,
    RelationshipExportEvidence,
    RelationshipExportFence,
    RelationshipExportFenceValidation,
    RelationshipExportPage,
    RelationshipExportRead,
    RelationshipPage,
    RelationshipRead,
    RelationshipSnapshot,
    RelationshipSourceExportFence,
)
from modules.knowledge.relationships.seed import (
    ensure_demo_relationships,  # re-export: used by documents seed
)
from modules.sources import public as sources
from modules.sources.schemas import SourceExportFence


def _actor(scope: Scope) -> int:
    """Return the principal recorded by a real workspace or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before relationship locks."""
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit relationship workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "RelationshipSnapshot",
    "ensure_demo_relationships",
]

MAX_CLEANUP_SUPPORTS = 10_000


def _evidence_scope(scope: Scope) -> ColumnElement[bool]:
    """Scope support rows through their workspace-owned relationship; the child has no scope column."""
    owned = aliased(Relationship)  # aliased: never auto-correlate with an enclosing Relationship query
    return RelationshipEvidence.relationship_id.in_(
        select(owned.id).where(owned.workspace_id == scope.workspace_id)
    )


async def record_relationship_history(
    session: AsyncSession, relationship_id: UUID, *, deleted: bool = False, scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Flush one presently observed owner snapshot; never backdate canonical truth.

    Caller holds canonical mutation fences and commits. Raw citation excerpts,
    titles and URLs are excluded; derived state is physically cleared on purge.
    Complete exact support is bounded and no missing historical state is invented.
    """
    snapshot = await get_relationship_snapshot(session, relationship_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if snapshot is None:
        return
    session.add(RelationshipSnapshotHistory(
        workspace_id=scope.workspace_id, relationship_id=relationship_id, deleted=deleted,
        state=snapshot.relationship.model_dump(mode="json", exclude={"evidence"}),
        support=snapshot.supports,
    ))
    await session.flush()


async def purge_history_support(session: AsyncSession, refs: list[tuple[UUID, UUID]]) -> None:
    """Remove purged evidence from every retained history page without committing.

    Caller holds deletion source/document fences before support cascades. Derived
    fields sharing any removed support lose their entire state; owner-authored
    fields survive without removed citations. Identifier-only support remains.
    """
    if len(set(refs)) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Historical support purge exceeds atomic bound")
    pairs = {(str(version), str(chunk)) for version, chunk in refs}
    if not pairs:
        return
    after = 0
    # ponytail: JSON support scan; add a GIN index if retained history makes purge slow.
    while True:
        rows = list((await session.scalars(select(RelationshipSnapshotHistory).where(
            RelationshipSnapshotHistory.id > after,
            or_(*[RelationshipSnapshotHistory.support.contains([{
                "document_version_id": version, "chunk_id": chunk,
            }]) for version, chunk in pairs]),
        ).order_by(RelationshipSnapshotHistory.id).limit(100).with_for_update())).all())
        if not rows:
            break
        for row in rows:
            row.support = [item for item in row.support if (
                str(item.get("document_version_id")), str(item.get("chunk_id")),
            ) not in pairs]
            if row.state.get("origin") != "owner":
                row.state = {}  # Past derived text cannot survive evidence purge.
        after = rows[-1].id
        await session.flush()


async def _schedule_relationship_change(
    session: AsyncSession, relationship_id: UUID, fields: list[str], *, deleted: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Queue detached exact support in the same owner transaction before commit."""
    from modules.knowledge.temporal import public as temporal
    row = await session.scalar(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
    ))
    if row is None:
        return
    support = list((await session.execute(select(
        RelationshipEvidence.document_version_id, RelationshipEvidence.chunk_id,
    ).where(_evidence_scope(scope),
            RelationshipEvidence.relationship_id == relationship_id).limit(MAX_CLEANUP_SUPPORTS + 1))).all())
    if len(support) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship change exceeds complete support bound")
    await temporal.schedule_canonical_change(
        session, kind="relationship", canonical_id=relationship_id, revision=None,
        fields=fields, support=[(version, chunk) for version, chunk in support],
        origin=row.origin, deleted=deleted, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def get_relationship_snapshot(
    session: AsyncSession, relationship_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> RelationshipSnapshot | None:
    """Detach complete current fact/support state under caller-held owner fences.

    Includes all support, endpoint revisions/redirects, exact memberships and
    source generations in deterministic digest. Missing/revoked provenance or
    over10000 support fails closed. Caller holds source/document then entity/fact
    publication locks when comparing for writes; this query never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
    ))
    if row is None:
        return None
    supports = list((await session.scalars(select(RelationshipEvidence).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == relationship_id,
    ).order_by(RelationshipEvidence.id).limit(MAX_CLEANUP_SUPPORTS + 1))).all())
    if len(supports) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship snapshot exceeds complete support bound")
    if row.origin == "derived" and not supports:
        raise LookupError("Derived relationship has no permitted support")
    evidence = []
    for offset in range(0, len(supports), 100):
        batch = await _evidence_read(session, supports[offset:offset + 100], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        if len(batch) != len(supports[offset:offset + 100]):
            raise LookupError("Relationship support is unavailable")
        evidence.extend(batch)
    endpoint_refs = await entities.get_entity_refs(session, [row.source_entity_id, row.target_entity_id],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    membership_ids = sorted({identifier for item in supports for identifier in (
        item.source_membership_id, item.target_membership_id,
    ) if identifier is not None})
    memberships = []
    for offset in range(0, len(membership_ids), 200):
        memberships.extend(await entities.get_membership_refs(session, membership_ids[offset:offset + 200],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled))
    by_id = {item.id: item for item in memberships}
    for item in supports:
        if row.origin == "derived" and (item.source_membership_id is None or item.target_membership_id is None):
            raise LookupError("Derived relationship endpoint support is unavailable")
        for identifier, endpoint in ((item.source_membership_id, row.source_entity_id), (item.target_membership_id, row.target_entity_id)):
            if identifier is not None:
                membership = by_id[identifier]
                if (membership.entity_id, membership.document_version_id, membership.chunk_id) != (endpoint, item.document_version_id, item.chunk_id):
                    raise LookupError("Relationship endpoint support changed")
    from modules.sources import public as sources
    generations = {}
    for identifier in sorted({item.source_id for item in evidence}):
        source = await sources.get_connector_source(session, identifier, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
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
        "supports": [{
            "id": str(item.id), "relationship_id": str(item.relationship_id),
            "document_id": str(item.document_id) if item.document_id else None,
            "source_id": str(item.source_id) if item.source_id else None,
            "document_version_id": str(item.document_version_id), "chunk_id": str(item.chunk_id),
            "observed_at": item.observed_at.isoformat() if item.observed_at else None,
            "extracted_at": item.extracted_at.isoformat(), "confidence": item.confidence,
            "source_membership_id": str(item.source_membership_id) if item.source_membership_id else None,
            "target_membership_id": str(item.target_membership_id) if item.target_membership_id else None,
        } for item in supports],
    }
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return RelationshipSnapshot(
        relationship=value, endpoints=deepcopy(payload["endpoints"]),
        memberships=deepcopy(payload["memberships"]), source_generations=generations, digest=digest,
        supports=deepcopy(payload["supports"]),
    )


async def _list_relationships_as_of(
    session: AsyncSession, *, limit: int, cursor: str | None, entity_id: UUID | None,
    valid_at: datetime | None, include_unknown_validity: bool,
    knowledge_as_of: datetime, fingerprint: str, scope: Scope, multi_workspace_enabled: bool,
) -> RelationshipPage:
    """Page actual latest recorded canonical snapshots by transaction cutoff.

    Current deletion/permission and retained exact support are checked again.
    Never return current fields as historical substitutes; unavailable intervals
    are explicit. Pagination covers snapshot identities, not loaded current rows.
    """
    ranked = select(
        RelationshipSnapshotHistory.id.label("history_id"),
        func.row_number().over(partition_by=RelationshipSnapshotHistory.relationship_id,
            order_by=(RelationshipSnapshotHistory.recorded_at.desc(), RelationshipSnapshotHistory.id.desc())).label("position"),
    ).where(RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
            RelationshipSnapshotHistory.recorded_at <= knowledge_as_of).subquery()
    missing_statement = select(Relationship.id).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.created_at <= knowledge_as_of,
        ~select(RelationshipSnapshotHistory.id).where(
        RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
        RelationshipSnapshotHistory.relationship_id == Relationship.id,
        RelationshipSnapshotHistory.recorded_at <= knowledge_as_of,
    ).exists())
    if entity_id is not None:
        missing_statement = missing_statement.where(or_(
            Relationship.source_entity_id == entity_id, Relationship.target_entity_id == entity_id,
        ))
    missing = list((await session.scalars(missing_statement.order_by(Relationship.id).limit(101))).all())
    statement = select(RelationshipSnapshotHistory).join(
        ranked, ranked.c.history_id == RelationshipSnapshotHistory.id,
    ).where(ranked.c.position == 1, RelationshipSnapshotHistory.workspace_id == scope.workspace_id)
    if entity_id is not None:
        statement = statement.where(or_(
            RelationshipSnapshotHistory.state["source_entity_id"].astext == str(entity_id),
            RelationshipSnapshotHistory.state["target_entity_id"].astext == str(entity_id),
        ))
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            bound, position = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if bound != fingerprint:
                raise ValueError
            recorded_at, identifier = decode_cursor(position)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid relationship history cursor") from exc
        statement = statement.where(tuple_(RelationshipSnapshotHistory.recorded_at,
            RelationshipSnapshotHistory.relationship_id) < (recorded_at, identifier))
    rows = list((await session.scalars(statement.order_by(
        RelationshipSnapshotHistory.recorded_at.desc(), RelationshipSnapshotHistory.relationship_id.desc(),
    ).limit(limit + 1))).all())
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            fingerprint, encode_cursor(rows[-1].recorded_at, rows[-1].relationship_id),
        ]).encode()).decode().rstrip("=")
    items, unavailable = [], []
    from modules.sources import public as sources
    for history in rows:
        if history.deleted:
            continue
        if not history.state or await session.scalar(select(Relationship.id).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id == history.relationship_id,
        )) is None:
            unavailable.append(history.relationship_id)
            continue
        item = RelationshipRead.model_validate(history.state)
        unknown = item.valid_from is None or item.valid_to is None
        if unknown and not include_unknown_validity:
            continue
        if valid_at is not None and ((item.valid_from is not None and item.valid_from > valid_at)
                                    or (item.valid_to is not None and item.valid_to <= valid_at)):
            continue
        try:
            endpoint_refs = await entities.get_entity_refs(
                session, [item.source_entity_id, item.target_entity_id], scope=scope,
                multi_workspace_enabled=multi_workspace_enabled)
            canonical_endpoints = {ref.requested_id: ref.canonical_id for ref in endpoint_refs}
            pairs = list(dict.fromkeys((UUID(str(s["document_version_id"])), UUID(str(s["chunk_id"]))) for s in history.support))
            if len(pairs) > MAX_CLEANUP_SUPPORTS:
                raise ValueError("Historical snapshot support exceeds its bound")
            refs = []
            for offset in range(0, len(pairs), 100):
                refs.extend(await documents.read_evidence_refs(
                    session, pairs[offset:offset + 100], scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled))
            permitted_sources = set()
            for source_id in sorted({ref.source_id for ref in refs}):
                source = await sources.get_connector_source(
                    session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                if source is not None and source.status == "active":
                    permitted_sources.add(source_id)
            by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in refs
                       if ref.source_id in permitted_sources and ref.observed_at <= knowledge_as_of}
            # Any missing support can have contributed to the retained derived fields.
            if item.origin == "derived" and (not pairs or len(by_pair) != len(pairs)):
                unavailable.append(history.relationship_id)
                continue
            membership_ids = sorted({UUID(str(s[key])) for s in history.support
                for key in ("source_membership_id", "target_membership_id") if s.get(key)})
            memberships = []
            for offset in range(0, len(membership_ids), 200):
                memberships.extend(await entities.get_membership_refs(
                    session, membership_ids[offset:offset + 200], scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled))
            by_membership = {member.id: member for member in memberships}
            evidence = []
            for support in history.support:
                pair = UUID(str(support["document_version_id"])), UUID(str(support["chunk_id"]))
                ref = by_pair.get(pair)
                if ref is None:
                    continue
                for key, endpoint in (("source_membership_id", item.source_entity_id),
                                      ("target_membership_id", item.target_entity_id)):
                    if not support.get(key):
                        if item.origin == "derived":
                            raise LookupError("Historical endpoint support unavailable")
                        continue
                    member = by_membership[UUID(str(support[key]))]
                    if (member.entity_id != canonical_endpoints[endpoint]
                            or (member.document_version_id, member.chunk_id) != pair):
                        raise LookupError("Historical endpoint support changed")
                evidence.append(EvidenceRead(
                    id=UUID(str(support["id"])), relationship_id=item.id,
                    document_id=ref.document_id, document_version_id=ref.document_version_id,
                    version_number=ref.version_number, chunk_id=ref.chunk_id,
                    observed_at=ref.observed_at, extracted_at=datetime.fromisoformat(str(support["extracted_at"])),
                    confidence=float(support["confidence"]),
                    source_entity_membership_id=UUID(str(support["source_membership_id"])) if support.get("source_membership_id") else None,
                    target_entity_membership_id=UUID(str(support["target_membership_id"])) if support.get("target_membership_id") else None,
                    title=ref.title, canonical_url=ref.canonical_url, source_id=ref.source_id,
                    excerpt=ref.excerpt, metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
                ))
            item.evidence = evidence
            items.append(item)
        except (LookupError, ValueError):
            unavailable.append(history.relationship_id)
    return RelationshipPage(items=items, next_cursor=next_cursor, knowledge_as_of=knowledge_as_of,
        canonical_history_available=not missing and not unavailable,
        observation_history_only=False, unavailable_relationship_ids=list(dict.fromkeys(unavailable + missing[:100])))


def _relationship_read(
    relationship: Relationship, evidence: list[EvidenceRead] | None = None
) -> RelationshipRead:
    """Project a stored relationship and optional evidence into its public DTO."""
    return RelationshipRead(
        id=relationship.id,
        source_entity_id=relationship.source_entity_id,
        target_entity_id=relationship.target_entity_id,
        type=relationship.type,
        origin=relationship.origin,
        confidence=relationship.confidence,
        valid_from=relationship.valid_from,
        valid_to=relationship.valid_to,
        metadata=relationship.metadata_json,
        created_at=relationship.created_at,
        evidence=evidence or [],
        validity_precision="bounded" if relationship.valid_from is not None and relationship.valid_to is not None else "unknown",
    )


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
    refs = await documents.read_evidence_refs(session, [(document_version_id, chunk_id)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    memberships = await entities.get_membership_refs(
        session, [source_membership_id, target_membership_id], for_write=True, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled
    )
    by_id = {item.id: item for item in memberships}
    source_membership, target_membership = by_id[source_membership_id], by_id[target_membership_id]
    for item, entity_id in ((source_membership, source_entity_id), (target_membership, target_entity_id)):
        if item.entity_id != entity_id or item.document_version_id != document_version_id or item.chunk_id != chunk_id:
            raise ValueError("Relationship evidence memberships do not match the cited chunk")
    ref = refs[0]
    relationship = await session.scalar(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id,
        Relationship.source_entity_id == source_entity_id,
        Relationship.target_entity_id == target_entity_id,
        Relationship.type == relationship_type,
        Relationship.origin == "derived",
        Relationship.valid_from.is_(None),
        Relationship.valid_to.is_(None),
    ).with_for_update())
    created = relationship is None
    if relationship is None:
        relationship = Relationship(
            workspace_id=scope.workspace_id, source_entity_id=source_entity_id, target_entity_id=target_entity_id,
            type=relationship_type, origin="derived", confidence=confidence,
        )
        session.add(relationship)
        await session.flush()
    evidence = await session.scalar(select(RelationshipEvidence).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == relationship.id,
        RelationshipEvidence.document_version_id == document_version_id,
        RelationshipEvidence.chunk_id == chunk_id,
        RelationshipEvidence.source_membership_id == source_membership_id,
        RelationshipEvidence.target_membership_id == target_membership_id,
    ).with_for_update())
    support_changed = evidence is None or confidence > evidence.confidence
    if evidence is None:
        session.add(RelationshipEvidence(
            relationship_id=relationship.id, document_version_id=document_version_id,
            chunk_id=chunk_id, document_id=ref.document_id, source_id=ref.source_id,
            observed_at=ref.observed_at, confidence=confidence,
            source_membership_id=source_membership_id, target_membership_id=target_membership_id,
        ))
    else:
        evidence.confidence = max(evidence.confidence, confidence)
    await session.flush()
    supported_confidence = await session.scalar(select(func.max(RelationshipEvidence.confidence)).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == relationship.id,
    ))
    confidence_changed = supported_confidence is not None and supported_confidence != relationship.confidence
    if supported_confidence is not None:
        relationship.confidence = supported_confidence
    await session.flush()
    if created or support_changed or confidence_changed:
        await record_relationship_history(session, relationship.id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
        await _schedule_relationship_change(session, relationship.id, ["support", "confidence"],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return relationship.id


async def _evidence_read(
    session: AsyncSession, rows: list[RelationshipEvidence], *, scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EvidenceRead]:
    """Join support rows to document-owned provenance and excerpts."""
    if not rows:
        return []
    pairs = list(dict.fromkeys((row.document_version_id, row.chunk_id) for row in rows))
    refs = await documents.read_evidence_refs(session, pairs, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in refs}
    return [
        EvidenceRead(
            id=row.id,
            relationship_id=row.relationship_id,
            document_id=ref.document_id,
            document_version_id=ref.document_version_id,
            version_number=ref.version_number,
            chunk_id=ref.chunk_id,
            observed_at=ref.observed_at,
            extracted_at=row.extracted_at,
            confidence=row.confidence,
            source_entity_membership_id=row.source_membership_id,
            target_entity_membership_id=row.target_membership_id,
            title=ref.title,
            canonical_url=ref.canonical_url,
            source_id=ref.source_id,
            excerpt=ref.excerpt,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
        )
        for row in rows
        if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
    ]


async def list_relationships(
    session: AsyncSession, limit: int, cursor: str | None, entity_id: UUID | None = None,
    *, valid_at: datetime | None = None, include_unknown_validity: bool = True,
    knowledge_as_of: datetime | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> RelationshipPage:
    """Page current canonical facts with half-open validity and observation cutoff.

    Null bounds mean unknown, not proven open validity. A knowledge cutoff returns
    actual retained owner snapshots and currently permitted evidence observed by
    then. Unrecorded/purged intervals are unavailable, never filled with current fields.
    Current permissions/deletion apply at every read; cursors bind all filters.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Relationship page limit must be between 1 and 100")
    for instant in (valid_at, knowledge_as_of):
        if instant is not None and (instant.tzinfo is None or instant.utcoffset() is None):
            raise ValueError("Relationship time controls require aware instants")
    valid_at = valid_at.astimezone(UTC) if valid_at else None
    knowledge_as_of = knowledge_as_of.astimezone(UTC) if knowledge_as_of else None
    if entity_id is not None:
        entity_id = await entities.resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    filters = [str(scope.workspace_id), str(entity_id) if entity_id else None, valid_at.isoformat() if valid_at else None,
               include_unknown_validity, knowledge_as_of.isoformat() if knowledge_as_of else None]
    fingerprint = sha256(json.dumps(filters, separators=(",", ":")).encode()).hexdigest()
    if knowledge_as_of is not None:
        return await _list_relationships_as_of(
            session, limit=limit, cursor=cursor, entity_id=entity_id, valid_at=valid_at,
            include_unknown_validity=include_unknown_validity,
            knowledge_as_of=knowledge_as_of, fingerprint=fingerprint, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    statement = select(Relationship).where(Relationship.workspace_id == scope.workspace_id)
    known = Relationship.valid_from.is_not(None) & Relationship.valid_to.is_not(None)
    if not include_unknown_validity:
        statement = statement.where(known)
    if valid_at is not None:
        in_interval = or_(Relationship.valid_from.is_(None), Relationship.valid_from <= valid_at) & or_(
            Relationship.valid_to.is_(None), Relationship.valid_to > valid_at,
        )
        statement = statement.where(in_interval)
    if entity_id is not None:
        statement = statement.where(
            or_(Relationship.source_entity_id == entity_id, Relationship.target_entity_id == entity_id)
        )
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            bound, position = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if bound != fingerprint:
                raise ValueError
            created_at, identifier = decode_cursor(position)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid relationship filter cursor") from exc
        statement = statement.where(tuple_(Relationship.created_at, Relationship.id) < (created_at, identifier))
    rows = list((await session.scalars(
        statement.order_by(desc(Relationship.created_at), desc(Relationship.id)).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            fingerprint, encode_cursor(rows[-1].created_at, rows[-1].id),
        ]).encode()).decode().rstrip("=")
    items = []
    for row in rows:
        try:
            snapshot = await get_relationship_snapshot(
                session, row.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        except LookupError:
            continue
        if snapshot is None:
            continue
        item = snapshot.relationship
        items.append(item)
    return RelationshipPage(items=items, next_cursor=next_cursor,
                            canonical_history_available=knowledge_as_of is None,
                            knowledge_as_of=knowledge_as_of,
                            observation_history_only=knowledge_as_of is not None)


async def list_relationship_evidence(
    session: AsyncSession, relationship_id: UUID, limit: int, cursor: str | None,
    *, knowledge_as_of: datetime | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[EvidenceRead] | None, str | None]:
    """Page retained observations independently of canonical historical availability.

    Current source permission/deletion still applies. An aware cutoff filters
    observed time, not fact validity or owner edits; cursor binds identity/cutoff.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Relationship evidence limit must be between 1 and 100")
    if knowledge_as_of is not None:
        if knowledge_as_of.tzinfo is None or knowledge_as_of.utcoffset() is None:
            raise ValueError("Evidence cutoff requires an aware instant")
        knowledge_as_of = knowledge_as_of.astimezone(UTC)
    fingerprint = sha256(json.dumps([str(relationship_id), knowledge_as_of.isoformat() if knowledge_as_of else None]).encode()).hexdigest()
    if await session.scalar(select(Relationship.id).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
    )) is None:
        return None, None
    statement = select(RelationshipEvidence).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == relationship_id,
    )
    if knowledge_as_of is not None:
        statement = statement.where(RelationshipEvidence.observed_at <= knowledge_as_of)
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            bound, position = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if bound != fingerprint:
                raise ValueError
            extracted_at, identifier = decode_cursor(position)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid relationship observation cursor") from exc
        statement = statement.where(tuple_(RelationshipEvidence.extracted_at, RelationshipEvidence.id) > (extracted_at, identifier))
    rows = list((await session.scalars(
        statement.order_by(RelationshipEvidence.extracted_at, RelationshipEvidence.id).limit(limit + 1)
    )).all())
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            fingerprint, encode_cursor(rows[-1].extracted_at, rows[-1].id),
        ]).encode()).decode().rstrip("=")
    evidence = await _evidence_read(session, rows, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    permitted = set()
    for source_id in sorted({ref.source_id for ref in evidence}):
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if source is not None and source.status == "active":
            permitted.add(source_id)
    return [ref for ref in evidence if ref.source_id in permitted
            and (knowledge_as_of is None or ref.observed_at <= knowledge_as_of)], next_cursor


def _encode_neighbor_cursor(focus_id: UUID, created_at: datetime, relationship_id: UUID) -> str:
    """Bind a relationship cursor to its focus entity before encoding it."""
    raw = json.dumps([str(focus_id), encode_cursor(created_at, relationship_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_neighbor_cursor(cursor: str, focus_id: UUID) -> tuple[object, UUID]:
    """Decode a canonical cursor and reject cursors created for another entity."""
    if len(cursor) > 512 or "=" in cursor:
        raise ValueError("Invalid neighbor cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Invalid neighbor cursor")
        focus, cursor_value = json.loads(raw)
        if focus != str(focus_id):
            raise ValueError("Neighbor cursor belongs to another entity")
        return decode_cursor(cursor_value)
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid neighbor cursor") from exc


async def get_neighbors(
    session: AsyncSession, entity_id: UUID, limit: int = 50, cursor: str | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> NeighborPage | None:
    """Return bounded adjacent entities with their connecting relationships."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 2 <= limit <= 100:
        raise ValueError("Neighbor page limit must be between 2 and 100 total nodes")
    try:
        focus = (await entities.get_entity_refs(
            session, [entity_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled))[0].canonical_id
    except LookupError:
        return None
    statement = select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id,
        or_(Relationship.source_entity_id == focus, Relationship.target_entity_id == focus),
    )
    if cursor:
        created_at, relationship_id = _decode_neighbor_cursor(cursor, focus)
        statement = statement.where(tuple_(Relationship.created_at, Relationship.id) < (created_at, relationship_id))
    rows = list((await session.scalars(
        statement.order_by(desc(Relationship.created_at), desc(Relationship.id)).limit(limit + 1)
    )).all())
    truncated = len(rows) > limit - 1
    rows = rows[: limit - 1]
    neighbor_ids = list(dict.fromkeys(
        row.target_entity_id if row.source_entity_id == focus else row.source_entity_id for row in rows
    ))
    refs = await entities.get_entity_refs(
        session, neighbor_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) if neighbor_ids else []
    by_id = {ref.requested_id: ref for ref in refs}
    items = [
        NeighborRead(
            entity=EntityGraphRead(
            id=by_id[neighbor_id].canonical_id, type=by_id[neighbor_id].type,
                name=by_id[neighbor_id].name, revision=by_id[neighbor_id].revision,
            ),
            relationship=_relationship_read(row),
        )
        for row in rows
        if (neighbor_id := (row.target_entity_id if row.source_entity_id == focus else row.source_entity_id)) in by_id
    ]
    next_cursor = _encode_neighbor_cursor(focus, rows[-1].created_at, rows[-1].id) if truncated and rows else None
    return NeighborPage(items=items, truncated=truncated, next_cursor=next_cursor)


async def create_relationship(
    session: AsyncSession, payload: RelationshipCreate, *, scope: Scope, multi_workspace_enabled: bool,
) -> RelationshipRead:
    """Create and commit a relationship with exact endpoint/evidence support.

    The workspace owner scope is admitted under the access-fence lock first and
    records the audit actor. The input may be owner or derived origin. Derived relationships
    require both endpoint memberships for each evidence ref and use the maximum
    evidence confidence; the function locks write refs and commits audit plus
    graph notification, retained canonical snapshot and temporal desired state.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    if payload.source_entity_id == payload.target_entity_id:
        raise ValueError("Relationship endpoints must be different")
    if payload.origin == "derived" and not payload.evidence:
        raise ValueError("Derived relationships require at least one evidence reference")
    pairs = list(dict.fromkeys((item.document_version_id, item.chunk_id) for item in payload.evidence))
    support_pairs = [(
        item.document_version_id, item.chunk_id,
        item.source_membership_id, item.target_membership_id,
    ) for item in payload.evidence]
    if len(set(support_pairs)) != len(support_pairs):
        raise ValueError("Relationship evidence membership pairs must be unique")
    evidence_refs = await documents.read_evidence_refs(
        session, pairs, for_write=bool(pairs), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in evidence_refs}
    endpoints = [payload.source_entity_id, payload.target_entity_id]
    try:
        await entities.get_entity_refs(
            session, endpoints, for_write=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except entities.TerminalEntityConflict as exc:
        raise LookupError("Relationship entity not found") from exc
    except LookupError as exc:
        raise LookupError("Relationship entity not found") from exc
    membership_ids = sorted({
        identifier
        for item in payload.evidence
        for identifier in (item.source_membership_id, item.target_membership_id)
        if identifier is not None
    }, key=str)
    try:
        memberships = await entities.get_membership_refs(
            session, membership_ids, for_write=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except LookupError as exc:
        raise ValueError("Relationship endpoint evidence membership is missing") from exc
    memberships_by_id = {item.id: item for item in memberships}
    evidence_rows: list[RelationshipEvidence] = []
    for item in payload.evidence:
        pair = (item.document_version_id, item.chunk_id)
        ref = by_pair.get(pair)
        if ref is None:
            raise ValueError("Relationship evidence does not exist")
        for membership_id, expected_entity_id in (
            (item.source_membership_id, payload.source_entity_id),
            (item.target_membership_id, payload.target_entity_id),
        ):
            membership = memberships_by_id.get(membership_id) if membership_id else None
            if payload.origin == "derived" and membership is None:
                raise ValueError("Derived evidence must identify both endpoint memberships")
            if membership is not None and (
                membership.entity_id != expected_entity_id
                or (membership.document_version_id, membership.chunk_id) != pair
            ):
                raise ValueError("Evidence membership does not match the relationship endpoint and evidence")
        evidence_rows.append(RelationshipEvidence(
            document_version_id=item.document_version_id,
            chunk_id=item.chunk_id,
            document_id=ref.document_id,
            source_id=ref.source_id,
            observed_at=ref.observed_at,
            confidence=item.confidence,
            source_membership_id=item.source_membership_id,
            target_membership_id=item.target_membership_id,
        ))
    relationship = Relationship(
        workspace_id=scope.workspace_id,
        source_entity_id=payload.source_entity_id,
        target_entity_id=payload.target_entity_id,
        type=payload.type,
        origin=payload.origin,
        confidence=(max((item.confidence for item in payload.evidence), default=None)
                    if payload.origin == "derived" else payload.confidence),
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        metadata_json=payload.metadata,
    )
    session.add(relationship)
    await session.flush()
    for evidence in evidence_rows:
        evidence.relationship_id = relationship.id
    session.add_all(evidence_rows)
    await session.flush()
    evidence_read = await _evidence_read(
        session, evidence_rows, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    result = _relationship_read(relationship, evidence_read)
    await entities.record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_create", reason=payload.reason,
        affected_ids=[relationship.id, *endpoints],
    )
    await record_relationship_history(
        session, relationship.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await _schedule_relationship_change(
        session, relationship.id, ["created", "support"], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(
        session, [make_graph_change(scope=scope, relationship_id=relationship.id)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return result


async def remove_relationship(
    session: AsyncSession, relationship_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    reason: str = "owner_relationship_delete",
) -> bool:
    """Delete a relationship after endpoint locks and commit its owner audit.

    The workspace owner scope is admitted under the access-fence lock first and
    is the audit actor. Returns False when the relationship is absent, rejects terminal
    endpoints, and captures exact support/history before deleting. Desired-state
    deletion, audit and replay commit atomically; no external graph work occurs.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    hint = await session.scalar(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
    ))
    if hint is None:
        return False
    endpoints = [hint.source_entity_id, hint.target_entity_id]
    try:
        await entities.get_entity_refs(
            session, endpoints, for_write=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except entities.TerminalEntityConflict as exc:
        raise LookupError("Relationship entity not found") from exc
    relationship = await session.scalar(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
        ).with_for_update()
    )
    if relationship is None:
        return False
    await record_relationship_history(
        session, relationship_id, deleted=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await _schedule_relationship_change(
        session, relationship_id, ["deleted"], deleted=True, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    await session.execute(update(RelationshipSnapshotHistory).where(
        RelationshipSnapshotHistory.workspace_id == scope.workspace_id,
        RelationshipSnapshotHistory.relationship_id == relationship_id,
    ).values(state={}, support=[]))
    await session.delete(relationship)
    await entities.record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_delete", reason=reason,
        affected_ids=[relationship_id, *endpoints],
    )
    await commit_with_replay(
        session, [make_graph_change(scope=scope, relationship_id=relationship_id, deleted=True)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return True


async def support_cleanup_ids(
    session: AsyncSession, *, refs: list[tuple[UUID, UUID]], document_id: UUID | None = None,
    source_id: UUID | None = None, membership_ids: list[UUID] | None = None,
) -> tuple[list[UUID], list[UUID]]:
    """Resolve bounded relationships and endpoint entities affected by support cleanup."""
    if (document_id is None) == (source_id is None):
        raise ValueError("Specify one document or source")
    statement = select(RelationshipEvidence.relationship_id).where(
        or_(
            RelationshipEvidence.document_id == document_id if document_id else RelationshipEvidence.source_id == source_id,
            tuple_(RelationshipEvidence.document_version_id, RelationshipEvidence.chunk_id).in_(refs) if refs else false(),
            RelationshipEvidence.source_membership_id.in_(membership_ids) if membership_ids else false(),
            RelationshipEvidence.target_membership_id.in_(membership_ids) if membership_ids else false(),
        )
    )
    relation_ids = sorted(set((await session.scalars(statement.limit(MAX_CLEANUP_SUPPORTS + 1))).all()), key=str)
    if len(relation_ids) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship support cleanup exceeds its atomic limit")
    if not relation_ids:
        return [], []
    rows = (await session.execute(
        select(Relationship.source_entity_id, Relationship.target_entity_id)
        .where(Relationship.id.in_(relation_ids))
    )).all()
    entity_ids = sorted({identifier for row in rows for identifier in row}, key=str)
    return relation_ids, entity_ids


async def lock_relationship_ids(
    session: AsyncSession, relationship_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Lock a bounded sorted relationship set in one workspace for cleanup or correction."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(relationship_ids), key=str)
    if len(ids) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship support cleanup exceeds its atomic limit")
    if ids:
        await session.scalars(
            select(Relationship.id).where(
                Relationship.workspace_id == scope.workspace_id, Relationship.id.in_(ids),
            ).order_by(Relationship.id).with_for_update()
        )


async def lock_delete_closure(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[UUID], list[UUID]]:
    """Lock and return the exact incident edge/support IDs after entity locks."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Entity deletion closure exceeds its atomic entity limit")
    edges = list((await session.scalars(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            or_(Relationship.source_entity_id.in_(ids), Relationship.target_entity_id.in_(ids)),
        ).order_by(Relationship.id).limit(101).execution_options(populate_existing=True)
    )).all())
    if len(edges) > 100:
        raise ValueError("Entity deletion closure exceeds its atomic relationship limit")
    relationship_ids = [item.id for item in edges]
    supports = list((await session.scalars(
        select(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id.in_(relationship_ids),
        ).order_by(RelationshipEvidence.id).limit(201).execution_options(populate_existing=True)
    )).all()) if relationship_ids else []
    support_snapshot = [(
        item.id, item.relationship_id, item.document_version_id, item.chunk_id,
        item.source_membership_id, item.target_membership_id, item.confidence,
    ) for item in supports]
    if len(supports) > 200 or len({(item.document_version_id, item.chunk_id) for item in supports}) > 100:
        raise ValueError("Entity deletion closure exceeds its atomic support limit")
    await lock_relationship_ids(
        session, relationship_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    support_ids = sorted((item.id for item in supports), key=str)
    if support_ids:
        await session.scalars(select(RelationshipEvidence.id).where(
            _evidence_scope(scope), RelationshipEvidence.id.in_(support_ids)
        ).order_by(RelationshipEvidence.id).with_for_update().execution_options(populate_existing=True))
    refreshed_edges = list((await session.scalars(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            or_(Relationship.source_entity_id.in_(ids), Relationship.target_entity_id.in_(ids)),
        ).order_by(Relationship.id).execution_options(populate_existing=True)
    )).all())
    refreshed_supports = list((await session.scalars(
        select(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id.in_(relationship_ids),
        ).order_by(RelationshipEvidence.id).execution_options(populate_existing=True)
    )).all()) if relationship_ids else []
    if ([item.id for item in refreshed_edges] != relationship_ids
            or [item.id for item in refreshed_supports] != support_ids
            or support_snapshot
            != [(item.id, item.relationship_id, item.document_version_id, item.chunk_id,
                 item.source_membership_id, item.target_membership_id, item.confidence) for item in refreshed_supports]):
        raise ValueError("Entity deletion relationship closure changed; retry")
    return relationship_ids, support_ids


async def remove_entity_closure(
    session: AsyncSession, entity_ids: list[UUID], relationship_ids: list[UUID], support_ids: list[UUID],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Delete the verified incident relationship closure and its support rows."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(entity_ids), key=str)
    current = list((await session.scalars(
        select(Relationship.id).where(
            Relationship.workspace_id == scope.workspace_id,
            or_(Relationship.source_entity_id.in_(ids), Relationship.target_entity_id.in_(ids)),
        ).order_by(Relationship.id)
    )).all())
    if current != sorted(set(relationship_ids), key=str):
        raise ValueError("Entity deletion relationship closure changed; retry")
    await session.execute(delete(RelationshipEvidence).where(
        _evidence_scope(scope), RelationshipEvidence.id.in_(support_ids),
    ))
    await session.execute(delete(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id.in_(relationship_ids),
    ))


async def list_correction_relationship_refs(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope,
) -> list[CorrectionRelationshipRef]:
    """Read a bounded incident edge/support snapshot in one workspace for correction planning.

    The calling correction transaction already holds owner admission; this read adds no
    authorization of its own beyond the role-only owner guard, and restricts rows to the scoped workspace.
    """
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Correction entity closure exceeds its atomic limit")
    if not ids:
        return []
    edges = list((await session.scalars(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            or_(Relationship.source_entity_id.in_(ids), Relationship.target_entity_id.in_(ids)),
        ).order_by(Relationship.id).limit(101).execution_options(populate_existing=True)
    )).all())
    if len(edges) > 100:
        raise ValueError("Correction relationship closure exceeds its atomic limit")
    if len(edges) != len(set(entity_ids)) and any(
        edge.source_entity_id == edge.target_entity_id for edge in edges
    ):
        raise ValueError("Correction relationship closure contains a self relationship")
    supports = (await session.scalars(
        select(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id.in_([edge.id for edge in edges]),
        ).order_by(RelationshipEvidence.relationship_id, RelationshipEvidence.id).limit(201).execution_options(populate_existing=True)
    )).all() if edges else []
    if len(supports) > 200 or len({(row.document_version_id, row.chunk_id) for row in supports}) > 100:
        raise ValueError("Correction evidence closure exceeds its atomic limit")
    if len({
        identifier for row in supports
        for identifier in (row.source_membership_id, row.target_membership_id)
        if identifier is not None
    }) > 200:
        raise ValueError("Correction membership closure exceeds its atomic limit")
    by_edge: dict[UUID, list[CorrectionSupportRef]] = {}
    for row in supports:
        by_edge.setdefault(row.relationship_id, []).append(CorrectionSupportRef(
            id=row.id, document_id=row.document_id, source_id=row.source_id,
            document_version_id=row.document_version_id, chunk_id=row.chunk_id,
            source_membership_id=row.source_membership_id,
            target_membership_id=row.target_membership_id, confidence=row.confidence,
        ))
    return [CorrectionRelationshipRef(
        id=edge.id, source_entity_id=edge.source_entity_id,
        target_entity_id=edge.target_entity_id, type=edge.type, origin=edge.origin,
        valid_from=edge.valid_from, valid_to=edge.valid_to, metadata=edge.metadata_json,
        supports=by_edge.get(edge.id, []),
    ) for edge in edges]


async def lock_correction_closure(
    session: AsyncSession, entity_ids: list[UUID], expected_relationship_ids: set[UUID],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Lock and revalidate the relationship/support snapshot before correction writes."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, entity_ids, scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    relation_ids = sorted(expected_relationship_ids, key=str)
    if relation_ids:
        await session.scalars(select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id.in_(relation_ids),
        ).order_by(Relationship.id).with_for_update().execution_options(populate_existing=True))
        support_ids = sorted({support.id for ref in refs for support in ref.supports}, key=str)
        if support_ids:
            await session.scalars(select(RelationshipEvidence).where(
                _evidence_scope(scope), RelationshipEvidence.id.in_(support_ids),
            ).order_by(RelationshipEvidence.id).with_for_update().execution_options(populate_existing=True))
        refreshed = await list_correction_relationship_refs(session, entity_ids, scope=scope)
        if [item.model_dump(mode="json") for item in refs] != [item.model_dump(mode="json") for item in refreshed]:
            raise ValueError("Correction relationship support closure changed; retry preview")


def validate_entity_merge_plan(
    source_entity_id: UUID, target_entity_id: UUID,
    refs: list[CorrectionRelationshipRef],
    source_redirect_ids: set[UUID] | None = None,
) -> None:
    """Reject merge plans that create self-edges or conflicting metadata."""
    source_ids = {source_entity_id, *(source_redirect_ids or set())}
    groups: dict[tuple[object, ...], list[CorrectionRelationshipRef]] = {}
    for ref in refs:
        if ref.origin != "derived" and ref.supports:
            raise ValueError("Merge has unexpected evidence attached to an owner-authored relationship")
        source_id = target_entity_id if ref.source_entity_id in source_ids else ref.source_entity_id
        target_id = target_entity_id if ref.target_entity_id in source_ids else ref.target_entity_id
        if source_id == target_id:
            raise ValueError("Merge would create a self relationship")
        key = (source_id, target_id, ref.type, ref.origin, ref.valid_from, ref.valid_to)
        groups.setdefault(key, []).append(ref)
    for group in groups.values():
        if any(ref.metadata != group[0].metadata for ref in group[1:]):
            raise ValueError("Merge has conflicting relationship metadata")


def validate_entity_split_plan(
    entity_id: UUID, membership_ids: set[UUID],
    refs: list[CorrectionRelationshipRef],
) -> None:
    """Reject split plans with unresolved endpoint support or self-edges."""
    for ref in refs:
        if ref.origin == "owner" and ref.supports:
            raise ValueError("Split has unexpected evidence attached to an owner-authored relationship")
        if ref.supports and any(
            support.source_membership_id is None or support.target_membership_id is None
            for support in ref.supports
        ):
            raise ValueError("Split has unresolved legacy relationship endpoint bindings")
        for support in ref.supports:
            moves_source = support.source_membership_id in membership_ids
            moves_target = support.target_membership_id in membership_ids
            if moves_source and moves_target:
                raise ValueError("Split would create a self relationship")


async def apply_entity_merge(
    session: AsyncSession, source_entity_id: UUID, target_entity_id: UUID,
    expected_relationship_ids: set[UUID], closure_entity_ids: list[UUID],
    source_redirect_ids: set[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID]]:
    """Redirect incident edges and retain presently observed post-correction history.

    Caller fences and captures old history before membership moves, then commits
    correction scheduling. This owner helper flushes; it never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, closure_entity_ids, scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    validate_entity_merge_plan(source_entity_id, target_entity_id, refs, source_redirect_ids)
    relation_ids = sorted(expected_relationship_ids, key=str)
    rows = list((await session.scalars(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id.in_(relation_ids),
    ).order_by(Relationship.id))).all()) if relation_ids else []
    source_merge_ids = {source_entity_id} | source_redirect_ids
    key_groups: dict[tuple[UUID, UUID, str, str, datetime | None, datetime | None], list[Relationship]] = {}
    for row in rows:
        next_source = target_entity_id if row.source_entity_id in ({source_entity_id} | source_redirect_ids) else row.source_entity_id
        next_target = target_entity_id if row.target_entity_id in ({source_entity_id} | source_redirect_ids) else row.target_entity_id
        key = (next_source, next_target, row.type, row.origin, row.valid_from, row.valid_to)
        key_groups.setdefault(key, []).append(row)
    replacements: list[tuple[UUID, UUID]] = []
    affected_relationship_ids: set[UUID] = set()
    for (source_id, target_id, _, _, _, _), group in key_groups.items():
        group.sort(key=lambda row: (bool(source_merge_ids.intersection((row.source_entity_id, row.target_entity_id))), str(row.id)))
        survivor = group[0]
        survivor.source_entity_id, survivor.target_entity_id = source_id, target_id
        affected_relationship_ids.add(survivor.id)
        supports_by_key: dict[tuple[UUID, UUID, UUID | None, UUID | None], RelationshipEvidence] = {}
        for row in group:
            support_rows = (await session.scalars(
                select(RelationshipEvidence).where(
                    _evidence_scope(scope),
                    RelationshipEvidence.relationship_id == row.id,
                ).order_by(RelationshipEvidence.id)
            )).all()
            for support in support_rows:
                support_key = (support.document_version_id, support.chunk_id, support.source_membership_id, support.target_membership_id)
                if support_key in supports_by_key:
                    keep = supports_by_key[support_key]
                    keep.confidence = max(keep.confidence, support.confidence)
                    await session.delete(support)
                else:
                    supports_by_key[support_key] = support
                    support.relationship_id = survivor.id
            if row.id != survivor.id:
                replacements.append((row.id, survivor.id))
                await session.delete(row)
    await _refresh_derived_confidence(session, affected_relationship_ids, scope=scope)
    await session.flush()
    for identifier in sorted(affected_relationship_ids):
        await record_relationship_history(
            session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    for old_id, _ in replacements:
        session.add(RelationshipSnapshotHistory(
            workspace_id=scope.workspace_id, relationship_id=old_id, deleted=True, state={}, support=[],
        ))
    return replacements


async def apply_entity_split(
    session: AsyncSession, entity_id: UUID, new_entity_id: UUID,
    membership_ids: set[UUID], expected_relationship_ids: set[UUID],
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID]]:
    """Move exact endpoint support and retain observed post-correction history.

    Caller captures old history before memberships move and owns correction
    scheduling/commit. Missing old identities receive identifier-only tombstones.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    refs = await list_correction_relationship_refs(session, [entity_id], scope=scope)
    if {item.id for item in refs} != expected_relationship_ids:
        raise ValueError("Correction relationship closure changed; retry preview")
    validate_entity_split_plan(entity_id, membership_ids, refs)
    relation_ids = sorted(expected_relationship_ids, key=str)
    rows = list((await session.scalars(select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id, Relationship.id.in_(relation_ids),
    ).order_by(Relationship.id))).all()) if relation_ids else []
    support_by_edge: dict[UUID, list[RelationshipEvidence]] = {}
    for support in (await session.scalars(select(RelationshipEvidence).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id.in_(relation_ids),
    ).order_by(RelationshipEvidence.relationship_id, RelationshipEvidence.id))).all() if relation_ids else []:
        support_by_edge.setdefault(support.relationship_id, []).append(support)
    replacements: list[tuple[UUID, UUID]] = []
    affected_relationship_ids: set[UUID] = set()
    for row in rows:
        source_rows = support_by_edge.get(row.id, [])
        if not source_rows:
            continue
        if row.origin == "owner":
            raise ValueError("Split cannot move evidence from an owner-authored relationship")
        moving = [item for item in source_rows if item.source_membership_id in membership_ids or item.target_membership_id in membership_ids]
        if not moving:
            continue
        endpoints: dict[tuple[UUID, UUID], list[RelationshipEvidence]] = {}
        for support in moving:
            next_source = new_entity_id if support.source_membership_id in membership_ids else row.source_entity_id
            next_target = new_entity_id if support.target_membership_id in membership_ids else row.target_entity_id
            if next_source == next_target:
                raise ValueError("Split would create a self relationship")
            endpoints.setdefault((next_source, next_target), []).append(support)
        for (next_source, next_target), moved_supports in endpoints.items():
            existing = await session.scalar(select(Relationship).where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id != row.id,
                Relationship.source_entity_id == next_source,
                Relationship.target_entity_id == next_target,
                Relationship.type == row.type,
                Relationship.origin == row.origin,
                Relationship.valid_from.is_not_distinct_from(row.valid_from),
                Relationship.valid_to.is_not_distinct_from(row.valid_to),
            ).order_by(Relationship.id).limit(1))
            if existing is not None and existing.metadata_json != row.metadata_json:
                raise ValueError("Split has conflicting relationship metadata")
            destination = existing or Relationship(
                workspace_id=scope.workspace_id, source_entity_id=next_source, target_entity_id=next_target, type=row.type,
                origin=row.origin, confidence=None, valid_from=row.valid_from,
                valid_to=row.valid_to, metadata_json=row.metadata_json,
            )
            if existing is None:
                session.add(destination)
                await session.flush()
            for support in moved_supports:
                support.relationship_id = destination.id
            affected_relationship_ids.add(destination.id)
            affected_relationship_ids.add(row.id)
            replacements.append((row.id, destination.id))
        if len(source_rows) == len(moving):
            await session.delete(row)
    await _refresh_derived_confidence(session, affected_relationship_ids, scope=scope)
    await session.flush()
    for identifier in sorted(affected_relationship_ids):
        if await session.scalar(select(Relationship.id).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id == identifier,
        )) is None:
            session.add(RelationshipSnapshotHistory(
                workspace_id=scope.workspace_id, relationship_id=identifier, deleted=True, state={}, support=[],
            ))
        else:
            await record_relationship_history(
                session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return replacements


async def _refresh_derived_confidence(
    session: AsyncSession, relationship_ids: set[UUID], *, scope: Scope,
) -> None:
    """Recompute derived relationship confidence from remaining evidence rows."""
    if not relationship_ids:
        return
    await session.flush()
    for relationship_id in sorted(relationship_ids, key=str):
        relationship = await session.scalar(select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id == relationship_id,
        ).execution_options(populate_existing=True))
        if relationship is None or relationship.origin != "derived":
            continue
        relationship.confidence = await session.scalar(select(func.max(
            RelationshipEvidence.confidence
        )).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == relationship_id,
        ))


async def remove_document_support(
    session: AsyncSession, *, document_id: UUID, refs: list[tuple[UUID, UUID]], membership_ids: list[UUID]
) -> int:
    """Remove evidence rows scoped to a document and refresh affected derived edges."""
    return await _remove_support(session, document_id=document_id, refs=refs, membership_ids=membership_ids)


async def remove_source_support(
    session: AsyncSession, *, source_id: UUID, refs: list[tuple[UUID, UUID]], membership_ids: list[UUID]
) -> int:
    """Remove evidence rows scoped to a source and refresh affected derived edges."""
    return await _remove_support(session, source_id=source_id, refs=refs, membership_ids=membership_ids)


async def _remove_support(
    session: AsyncSession, *, refs: list[tuple[UUID, UUID]], membership_ids: list[UUID],
    document_id: UUID | None = None, source_id: UUID | None = None,
) -> int:
    """Delete bounded matching evidence and remove unsupported derived relationships."""
    statement = select(RelationshipEvidence).where(or_(
        RelationshipEvidence.document_id == document_id if document_id else RelationshipEvidence.source_id == source_id,
        tuple_(RelationshipEvidence.document_version_id, RelationshipEvidence.chunk_id).in_(refs) if refs else false(),
        RelationshipEvidence.source_membership_id.in_(membership_ids) if membership_ids else false(),
        RelationshipEvidence.target_membership_id.in_(membership_ids) if membership_ids else false(),
    )).limit(MAX_CLEANUP_SUPPORTS + 1)
    rows = list((await session.scalars(statement)).all())
    if len(rows) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship support cleanup exceeds its atomic limit")
    affected = sorted({row.relationship_id for row in rows}, key=str)
    await session.execute(delete(RelationshipEvidence).where(RelationshipEvidence.id.in_([row.id for row in rows])))
    for relationship_id in affected:
        relationship = await session.get(Relationship, relationship_id)
        if relationship is None or relationship.origin != "derived":
            continue
        remaining = list((await session.scalars(
            select(RelationshipEvidence).where(RelationshipEvidence.relationship_id == relationship_id)
        )).all())
        if not remaining:
            await session.delete(relationship)
        else:
            relationship.confidence = max(row.confidence for row in remaining)
    return len(rows)


RELATIONSHIP_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _encode_relationship_export_cursor(
    owner_id: int, workspace_id: UUID, snapshot_at: datetime, position_at: datetime, position_id: UUID,
) -> str:
    """Encode a canonical relationship cursor bound to owner and fixed snapshot."""
    raw = json.dumps({"v": 1, "owner": owner_id, "workspace": str(workspace_id), "kind": "relationships",
                      "snapshot": snapshot_at.astimezone(UTC).isoformat(),
                      "at": position_at.astimezone(UTC).isoformat(), "id": str(position_id)},
                     sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_relationship_export_cursor(cursor: str, owner_id: int, workspace_id: UUID) -> tuple[datetime, datetime, UUID]:
    """Reject malformed, overlong, future, or cross-owner relationship cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"v", "owner", "workspace", "kind", "snapshot", "at", "id"}:
            raise ValueError
        if value["v"] != 1 or value["owner"] != owner_id or value["workspace"] != str(workspace_id) or value["kind"] != "relationships":
            raise ValueError
        snapshot_at, position_at = datetime.fromisoformat(value["snapshot"]), datetime.fromisoformat(value["at"])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, position_at)):
            raise ValueError
        snapshot_at, position_at = snapshot_at.astimezone(UTC), position_at.astimezone(UTC)
        if snapshot_at > datetime.now(UTC):
            raise ValueError
        position_id = UUID(value["id"])
        if _encode_relationship_export_cursor(owner_id, workspace_id, snapshot_at, position_at, position_id) != cursor:
            raise ValueError
        return snapshot_at, position_at, position_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid relationship export cursor") from exc


def _relationship_export_bytes(items: list[RelationshipExportRead]) -> int:
    """Measure exact compact JSON bytes for the immutable relationship page."""
    return len(json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _relationship_export_statement(snapshot_at: datetime, *, scope: Scope) -> Select[Any]:
    """Select workspace relationships plus derived rows with retained eligible citations."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    retained_support = exists(select(RelationshipEvidence.id).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == Relationship.id,
        RelationshipEvidence.source_id.in_(eligible),
    ))
    return select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id,
        Relationship.created_at <= snapshot_at, Relationship.updated_at <= snapshot_at,
        or_(Relationship.origin == "owner", retained_support),
    )


async def _relationship_export_count(session: AsyncSession, snapshot_at: datetime, *, scope: Scope) -> int:
    """Count portable workspace relationships visible at the captured source-purge boundary."""
    statement = _relationship_export_statement(snapshot_at, scope=scope).with_only_columns(func.count()).order_by(None)
    return int(await session.scalar(statement) or 0)


async def _relationship_source_generations(
    session: AsyncSession, source_ids: set[UUID], *, scope: Scope,
) -> dict[UUID, int]:
    """Read eligible source generations through the owner-public lifecycle projection."""
    if not source_ids:
        return {}
    projection = sources.ingestion_lifecycle_projection(scope=scope).subquery()
    rows: Any = (await session.execute(select(projection.c.id, projection.c.generation).where(
        projection.c.id.in_(source_ids), projection.c.id.in_(sources.export_eligible_source_ids(scope=scope)),
    ))).all()
    result = {source_id: int(generation) for source_id, generation in rows}
    if result.keys() != source_ids:
        raise ValueError("Relationship citation source is purging or no longer retained")
    return result


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50,
    cursor: str | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> RelationshipExportPage:
    """Return canonical relationships and stable eligible citations in a bounded keyset page.

    Derived relationships require at least one citation from an export-eligible source;
    owner-authored relations survive independently, with purging citations withheld. Raw
    excerpts, URLs, metadata, provider payloads, and ORM entities from other modules are omitted.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "relationships" or not 1 <= limit <= 100:
        raise ValueError("Relationship export kind or page limit is invalid")
    if owner_id != _actor(scope):
        raise PermissionError("Relationship export requires the workspace owner")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_relationship_export_cursor(cursor, owner_id, scope.workspace_id)
        position = (position_at, position_id)
    snapshot_count = await _relationship_export_count(session, snapshot_at, scope=scope)
    statement = _relationship_export_statement(snapshot_at, scope=scope)
    if position is not None:
        statement = statement.where(tuple_(Relationship.created_at, Relationship.id) > position)
    rows = list((await session.scalars(statement.order_by(Relationship.created_at, Relationship.id)
                                       .limit(limit + 1).execution_options(populate_existing=True))).all())
    has_more = len(rows) > limit
    items: list[RelationshipExportRead] = []
    fences: list[RelationshipExportFence] = []
    eligible = sources.export_eligible_source_ids(scope=scope)
    for row in rows[:limit]:
        supports = list((await session.scalars(select(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == row.id,
            RelationshipEvidence.source_id.in_(eligible),
        ).order_by(RelationshipEvidence.id).limit(101)
          .execution_options(populate_existing=True))).all())
        if len(supports) > 100:
            raise ValueError("A relationship export record exceeds the citation bound")
        generations = await _relationship_source_generations(session, {
            support.source_id for support in supports if support.source_id is not None
        }, scope=scope)
        refs = [RelationshipExportEvidence(
            id=support.id, source_id=support.source_id,
            source_generation=generations.get(support.source_id) if support.source_id is not None else None,
            document_id=support.document_id, document_version_id=support.document_version_id,
            chunk_id=support.chunk_id, source_membership_id=support.source_membership_id,
            target_membership_id=support.target_membership_id, observed_at=support.observed_at,
            confidence=support.confidence,
        ) for support in supports]
        item = RelationshipExportRead(
            id=row.id, source_entity_id=row.source_entity_id, target_entity_id=row.target_entity_id,
            type=row.type, origin=row.origin, confidence=row.confidence, valid_from=row.valid_from,
            valid_to=row.valid_to, created_at=row.created_at, updated_at=row.updated_at, evidence=refs,
        )
        if _relationship_export_bytes(items + [item]) > RELATIONSHIP_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("A relationship export record exceeds the page byte budget")
            has_more = True
            break
        items.append(item)
        digest = sha256(json.dumps([ref.model_dump(mode="json") for ref in refs], ensure_ascii=False,
                                   separators=(",", ":")).encode()).hexdigest()
        fences.append(RelationshipExportFence(
            id=row.id, created_at=row.created_at, updated_at=row.updated_at,
            evidence_ids=[ref.id for ref in refs],
            source_fences=[RelationshipSourceExportFence(source_id=source_id, generation=generation)
                           for source_id, generation in sorted(generations.items(), key=lambda pair: str(pair[0]))],
            evidence_digest=digest,
        ))
    next_cursor = (_encode_relationship_export_cursor(owner_id, scope.workspace_id, snapshot_at, items[-1].created_at, items[-1].id)
                   if has_more and items else None)
    return RelationshipExportPage(
        owner_id=owner_id, record_kind="relationships", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=_relationship_export_bytes(items), max_payload_bytes=RELATIONSHIP_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[RelationshipExportFence], scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipExportFenceValidation:
    """Recheck owner count, canonical row state, source generations, and exact citations."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "relationships" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Relationship export revalidation input is invalid")
    if owner_id != _actor(scope):
        return RelationshipExportFenceValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    observed = await _relationship_export_count(session, snapshot_at, scope=scope)
    if observed != expected_snapshot_count:
        return RelationshipExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    eligible = sources.export_eligible_source_ids(scope=scope)
    for fence in fences:
        row = await session.scalar(select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id, Relationship.id == fence.id,
        ).execution_options(populate_existing=True))
        if row is None or (row.created_at, row.updated_at) != (fence.created_at, fence.updated_at):
            return RelationshipExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        source_fences = [SourceExportFence(source_id=item.source_id, workspace_id=scope.workspace_id, generation=item.generation)
                         for item in fence.source_fences]
        if set(await sources.filter_export_eligible_sources(
            session, source_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled)) != {
            item.source_id for item in source_fences
        }:
            return RelationshipExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        supports = list((await session.scalars(select(RelationshipEvidence).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == fence.id, RelationshipEvidence.source_id.in_(eligible),
        ).order_by(RelationshipEvidence.id).limit(101)
          .execution_options(populate_existing=True))).all())
        if len(supports) > 100:
            return RelationshipExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        generations = await _relationship_source_generations(session, {
            support.source_id for support in supports if support.source_id is not None
        }, scope=scope)
        refs = [RelationshipExportEvidence(
            id=support.id, source_id=support.source_id,
            source_generation=generations.get(support.source_id) if support.source_id is not None else None,
            document_id=support.document_id, document_version_id=support.document_version_id,
            chunk_id=support.chunk_id, source_membership_id=support.source_membership_id,
            target_membership_id=support.target_membership_id, observed_at=support.observed_at,
            confidence=support.confidence,
        ) for support in supports]
        digest = sha256(json.dumps([ref.model_dump(mode="json") for ref in refs], ensure_ascii=False,
                                   separators=(",", ":")).encode()).hexdigest()
        if [ref.id for ref in refs] != fence.evidence_ids or digest != fence.evidence_digest:
            return RelationshipExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return RelationshipExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)

