from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import base64
import binascii
from copy import deepcopy
import json
import math
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import delete, desc, func, or_, select, tuple_, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_graph_change
from modules.sources import public as sources
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityEvidenceMembership,
    EntityAliasEvidence,
    EntityFieldEvidence,
    EntityOwnerAction,
    EntityRedirect,
    EntityCorrectionDecision,
    EntityExtractionWork,
    EntityExtractionResult,
)
from modules.knowledge.entities.schemas import (
    EntityTemporalNodeSeed, EntityHistoryItem, EntityHistoryPage,
    AliasCreate,
    EntityAliasRead,
    EntityEvidencePage,
    EntityEvidenceRead,
    EntityReviewEvidence,
    EntityReviewEndpoint,
    EntityCreate,
    EntityMembershipReferenceRead,
    VersionMembershipReference,
    EntityPage,
    EntityPatch,
    EntityRead,
    EntityReferenceRead,
    EntityReviewCandidate,
    EntityReviewPage,
    EntityReviewAssignmentRequest,
    EntityReviewAssignmentResult,
    EntityRelationshipReviewRequest,
    EntityRelationshipReviewResult,
    canonicalize_name,
)


async def get_temporal_node_seeds(
    session: AsyncSession, membership_ids: list[UUID],
) -> tuple[EntityTemporalNodeSeed, ...]:
    """Prove current nonblank fields against exact selected source-local evidence.

    Requires caller-held source/document fences and current egress policy. Up to
    100 unique memberships must belong to one active source generation. Missing,
    stale, redirected or unsupported fields fail closed; no writes or commits.
    Owner authorship alone is not source-local evidence for model seed text.
    """
    if not membership_ids or len(membership_ids) > 100 or len(set(membership_ids)) != len(membership_ids):
        raise ValueError("Node seed memberships must contain 1 to 100 unique IDs")
    rows = list((await session.scalars(select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.id.in_(membership_ids),
    ).order_by(EntityEvidenceMembership.id))).all())
    if len(rows) != len(membership_ids) or len({row.source_id for row in rows}) != 1:
        raise LookupError("Node seed memberships are missing or cross-source")
    source = await sources.get_connector_source(session, rows[0].source_id)
    if source is None or source.status != "active":
        raise LookupError("Node seed source is unavailable")
    from modules.knowledge.documents import public as documents
    groups: dict[tuple[UUID, UUID], list[UUID]] = {}
    for row in rows:
        groups.setdefault((row.document_id, row.document_version_id), []).append(row.chunk_id)
    for (document_id, version_id), chunks in groups.items():
        fences = await documents.review_version_fences(session, [version_id])
        fence = fences.get(version_id)
        refs = await documents.read_evidence_refs(session, [
            (version_id, chunk) for chunk in dict.fromkeys(chunks)
        ])
        if (fence is None or fence.document_id != document_id or fence.source_id != source.id
                or fence.current_source_generation != source.generation
                or len(refs) != len(set(chunks))
                or any(ref.document_id != document_id or ref.source_id != source.id for ref in refs)):
            raise LookupError("Node seed retained evidence is not current and permitted")
    result = []
    for entity_id in sorted({row.entity_id for row in rows}):
        ref = (await get_entity_refs(session, [entity_id]))[0]
        if ref.canonical_id != entity_id:
            raise LookupError("Node seed identity was redirected")
        entity = await session.get(Entity, entity_id)
        if entity is None or not entity.name or not entity.name.strip() or entity.name_origin is None:
            raise LookupError("Node seed name is unavailable")
        selected = [row for row in rows if row.entity_id == entity_id]
        proofs = list((await session.scalars(select(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.membership_id.in_([row.id for row in selected]),
        ))).all())
        name_hash = sha256(entity.name.encode("utf-8")).hexdigest()
        name_support = sorted({p.membership_id for p in proofs if p.field_name == "name" and p.value_hash == name_hash})
        if not name_support:
            raise LookupError("Node seed name has no exact selected field support")
        summary_hash = sha256(entity.description.encode("utf-8")).hexdigest() if entity.description and entity.description.strip() and entity.description_origin else None
        summary_support = sorted({p.membership_id for p in proofs if p.field_name == "description" and p.value_hash == summary_hash})
        result.append(EntityTemporalNodeSeed(
            entity_id=entity.id, revision=entity.revision, type=entity.type,
            name=entity.name, summary=entity.description if summary_support else None,
            source_id=source.id, source_generation=source.generation,
            memberships=await get_membership_refs(session, [row.id for row in selected]),
            name_support_membership_ids=name_support, summary_support_membership_ids=summary_support,
            name_hash=name_hash, summary_hash=summary_hash if summary_support else None,
        ))
    if sum(len((seed.name + (seed.summary or "")).encode("utf-8")) for seed in result) > 64_000:
        raise ValueError("Node seed text exceeds its 64000-byte aggregate bound")
    return tuple(result)


async def list_entity_history(
    session: AsyncSession, entity_id: UUID, limit: int = 50, cursor: str | None = None,
    *, membership_cursor: str | None = None,
) -> EntityHistoryPage | None:
    """Page identifier-only owner audit for a currently accessible canonical entity.

    No reason/raw historical values are exposed. Audit timestamps describe edits,
    not occurrence; retained evidence remains available via list_entity_evidence.
    Bound cursor to requested identity and never manufacture past field values.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Entity history page limit must be between 1 and 100")
    try:
        canonical = await resolve_canonical_entity_id(session, entity_id)
    except LookupError:
        return None
    statement = select(EntityOwnerAction).where(or_(
        EntityOwnerAction.affected_ids.contains([str(entity_id)]),
        EntityOwnerAction.affected_ids.contains([str(canonical)]),
    ))
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            identity, position = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if identity != str(entity_id):
                raise ValueError
            timestamp, identifier = decode_cursor(position)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid entity history cursor") from exc
        statement = statement.where(tuple_(EntityOwnerAction.created_at, EntityOwnerAction.id) < (timestamp, identifier))
    rows = list((await session.scalars(statement.order_by(
        EntityOwnerAction.created_at.desc(), EntityOwnerAction.id.desc(),
    ).limit(limit + 1))).all())
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        position = encode_cursor(rows[-1].created_at, rows[-1].id)
        next_cursor = base64.urlsafe_b64encode(json.dumps([str(entity_id), position]).encode()).decode().rstrip("=")
    membership_position = None
    if membership_cursor:
        try:
            if len(membership_cursor) > 1024:
                raise ValueError
            identity, membership_position = json.loads(base64.b64decode(
                membership_cursor + "=" * (-len(membership_cursor) % 4), altchars=b"-_", validate=True,
            ))
            if identity != str(entity_id):
                raise ValueError
            decode_cursor(membership_position)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid entity membership history cursor") from exc
    membership_page = await list_entity_evidence(session, canonical, limit, membership_position)
    if membership_page:
        permitted = set()
        for source_id in sorted({item.source_id for item in membership_page.items}):
            source = await sources.get_connector_source(session, source_id)
            if source is not None and source.status == "active":
                permitted.add(source_id)
        membership_page.items = [item for item in membership_page.items if item.source_id in permitted]
    membership_next = None
    if membership_page and membership_page.next_cursor:
        membership_next = base64.urlsafe_b64encode(json.dumps([
            str(entity_id), membership_page.next_cursor,
        ]).encode()).decode().rstrip("=")
    return EntityHistoryPage(items=[EntityHistoryItem(
        id=row.id, recorded_at=row.created_at, operation=row.operation,
        affected_ids=row.affected_ids, revisions=row.revisions,
    ) for row in rows], next_cursor=next_cursor,
        memberships=membership_page.items if membership_page else [],
        membership_next_cursor=membership_next)


async def _schedule_entity_change(
    session: AsyncSession, entity: Entity, fields: list[str], origin: str,
) -> None:
    """Flush exact current support/revision scheduling in caller's canonical transaction."""
    from modules.knowledge.temporal import public as temporal
    rows = list((await session.execute(select(
        EntityEvidenceMembership.document_version_id, EntityEvidenceMembership.chunk_id,
    ).where(EntityEvidenceMembership.entity_id == entity.id).limit(10_001))).all())
    if len(rows) > 10_000:
        raise ValueError("Entity change exceeds complete support bound")
    await temporal.schedule_canonical_change(
        session, kind="entity", canonical_id=entity.id, revision=entity.revision,
        fields=fields, support=[(version, chunk) for version, chunk in rows], origin=origin,
    )


class RedirectedEntityConflict(ValueError):
    """Signal that a write used an entity ID redirected by a merge."""


class TerminalEntityConflict(LookupError):
    """Signal that an entity identity was deleted and cannot be followed."""
if TYPE_CHECKING:
    from modules.knowledge.documents.public import ExtractionEvidenceRef


def _entity_read(entity: Entity, aliases: list[EntityAlias] | None = None) -> EntityRead:
    """Project an entity and aliases while hiding fields without known provenance."""
    return EntityRead(
        id=entity.id,
        type=entity.type,
        name=entity.name if entity.name_origin is not None else None,
        canonical_name=entity.canonical_name if entity.name_origin is not None else None,
        description=entity.description if entity.description_origin is not None else None,
        metadata=entity.metadata_json,
        revision=entity.revision,
        name_origin=entity.name_origin,
        description_origin=entity.description_origin,
        first_seen_at=entity.first_seen_at,
        last_seen_at=entity.last_seen_at,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
        aliases=[
            EntityAliasRead(
                id=item.id,
                entity_id=item.entity_id,
                alias=item.alias,
                source_id=item.source_id,
                confirmed=item.confirmed,
                origin=item.origin,
                confidence=item.confidence,
                created_at=item.created_at,
            )
            for item in aliases or []
        ],
    )


async def _aliases(session: AsyncSession, entity_ids: list[UUID]) -> dict[UUID, list[EntityAlias]]:
    """Load ordered display aliases, excluding unconfirmed source-derived aliases."""
    if not entity_ids:
        return {}
    result: dict[UUID, list[EntityAlias]] = {}
    aliases = (await session.scalars(
        select(EntityAlias).where(
            EntityAlias.entity_id.in_(entity_ids),
            or_(EntityAlias.origin.is_not(None), EntityAlias.source_id.is_(None)),
        ).order_by(EntityAlias.alias)
    )).all()
    for alias in aliases:
        result.setdefault(alias.entity_id, []).append(alias)
    return result


async def record_owner_action(
    session: AsyncSession,
    *,
    actor_id: int,
    operation: str,
    reason: str,
    affected_ids: list[UUID],
    revisions: dict[str, int | None] | None = None,
) -> None:
    """Queue an owner correction audit record without committing the transaction."""
    clean_reason = " ".join(reason.split())
    if not clean_reason or len(clean_reason) > 300:
        raise ValueError("Owner action reason must contain 1 to 300 characters")
    session.add(EntityOwnerAction(
        actor_id=actor_id,
        operation=operation,
        reason=clean_reason,
        affected_ids=[str(identifier) for identifier in affected_ids],
        revisions=revisions or {},
        created_at=datetime.now(UTC),
    ))


async def list_entities(
    session: AsyncSession, limit: int, cursor: str | None, entity_type: str | None, query: str | None
) -> EntityPage:
    """Return a cursor-paged list of canonical entities matching optional filters."""
    statement = select(Entity).where(~Entity.id.in_(select(EntityRedirect.old_entity_id)))
    if entity_type:
        statement = statement.where(Entity.type == entity_type)
    if query:
        statement = statement.where(
            or_(Entity.name.ilike(f"%{query}%"), Entity.canonical_name.ilike(f"%{canonicalize_name(query)}%"))
        )
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Entity.created_at, Entity.id) < (created_at, identifier))
    rows = list((await session.scalars(
        statement.order_by(desc(Entity.created_at), desc(Entity.id)).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    aliases = await _aliases(session, [row.id for row in rows])
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return EntityPage(items=[_entity_read(row, aliases.get(row.id)) for row in rows], next_cursor=next_cursor)


async def get_entity(session: AsyncSession, entity_id: UUID) -> EntityRead | None:
    """Read a canonical entity through redirects, returning None when unavailable."""
    try:
        canonical_id = await resolve_canonical_entity_id(session, entity_id)
    except LookupError:
        return None
    entity = await session.get(Entity, canonical_id)
    if entity is None:
        return None
    aliases = await _aliases(session, [entity.id])
    return _entity_read(entity, aliases.get(entity.id))


async def get_entity_refs(
    session: AsyncSession, ids: list[UUID], *, for_write: bool = False
) -> list[EntityReferenceRead]:
    """Resolve up to 100 unique entity IDs while preserving requested order.

    Read mode follows redirects. Write mode rejects redirected and terminally
    deleted IDs, then locks canonical rows in sorted order. Missing references
    raise LookupError; duplicate or oversized input raises ValueError.
    """
    if len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError("Entity reference query must contain up to 100 unique IDs")
    if not ids:
        return []
    canonical_ids: dict[UUID, UUID] = {}
    for identifier in ids:
        try:
            canonical_ids[identifier] = await resolve_canonical_entity_id(session, identifier)
        except LookupError as exc:
            if for_write and await session.scalar(select(EntityRedirect.old_entity_id).where(EntityRedirect.old_entity_id == identifier)) is not None:
                raise TerminalEntityConflict("Entity identity was deleted") from exc
            raise
    if for_write and any(canonical_ids[identifier] != identifier for identifier in ids):
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    query = select(Entity).where(Entity.id.in_(set(canonical_ids.values())))
    if for_write:
        query = query.order_by(Entity.id).with_for_update()
    rows = (await session.scalars(query)).all()
    rows_by_id = {row.id: row for row in rows}
    by_id = {
        requested_id: EntityReferenceRead(
            requested_id=requested_id, canonical_id=canonical_ids[requested_id], revision=rows_by_id[canonical_ids[requested_id]].revision, type=rows_by_id[canonical_ids[requested_id]].type,
            name=rows_by_id[canonical_ids[requested_id]].name if rows_by_id[canonical_ids[requested_id]].name_origin is not None else None,
        )
        for requested_id in ids if canonical_ids[requested_id] in rows_by_id
    }
    if set(by_id) != set(ids):
        raise LookupError("Entity reference is missing")
    return [by_id[identifier] for identifier in ids]


async def resolve_canonical_entity_id(session: AsyncSession, entity_id: UUID) -> UUID:
    """Follow the bounded owner redirect chain; malformed cycles fail closed."""
    current = entity_id
    seen = {current}
    for _ in range(32):
        redirect = await session.scalar(select(EntityRedirect).where(EntityRedirect.old_entity_id == current))
        if redirect is None:
            if await session.get(Entity, current) is None:
                raise LookupError("Entity reference is missing")
            return current
        target = redirect.target_entity_id
        if target is None:
            raise LookupError("Entity identity was deleted")
        if target in seen:
            raise ValueError("Entity redirect cycle detected")
        seen.add(target)
        current = target
    raise ValueError("Entity redirect chain exceeds its limit")


async def get_membership_refs(
    session: AsyncSession, ids: list[UUID], *, for_write: bool = False
) -> list[EntityMembershipReferenceRead]:
    """Resolve unique memberships, acquiring stable entity/ID locks for writes."""
    if len(ids) > 200 or len(set(ids)) != len(ids):
        raise ValueError("Entity membership query must contain up to 200 unique IDs")
    if not ids:
        return []
    query = select(EntityEvidenceMembership).where(EntityEvidenceMembership.id.in_(ids))
    if for_write:
        query = query.order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).with_for_update().execution_options(populate_existing=True)
    rows = (await session.scalars(query)).all()
    by_id = {
        row.id: EntityMembershipReferenceRead(
            id=row.id, entity_id=row.entity_id, document_version_id=row.document_version_id,
            chunk_id=row.chunk_id, observed_at=row.observed_at, extracted_at=row.extracted_at,
            confidence=row.confidence,
        )
        for row in rows
    }
    if set(by_id) != set(ids):
        raise LookupError("Entity evidence membership is missing")
    return [by_id[identifier] for identifier in ids]


async def list_version_membership_refs(
    session: AsyncSession, document_version_id: UUID, chunk_ids: list[UUID]
) -> list[VersionMembershipReference]:
    """Return bounded canonical memberships for chunks already authorized by documents extraction input.

    The caller must keep the documents source/document egress fence and must
    validate each returned chunk against that extraction input. The detached
    result exposes membership keys for model selection; the model never chooses
    a global entity ID.
    """
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Version membership chunks must be unique and bounded")
    if not chunk_ids:
        return []
    rows = (await session.execute(
        select(EntityEvidenceMembership, Entity)
        .join(Entity, Entity.id == EntityEvidenceMembership.entity_id)
        .where(
            EntityEvidenceMembership.document_version_id == document_version_id,
            EntityEvidenceMembership.chunk_id.in_(chunk_ids),
            ~EntityEvidenceMembership.entity_id.in_(select(EntityRedirect.old_entity_id)),
        )
        .order_by(EntityEvidenceMembership.id)
    )).all()
    if len(rows) > 150:
        raise ValueError("Version membership context exceeds 150 exact supports")
    return [VersionMembershipReference(
        membership_id=membership.id, entity_id=entity.id, entity_type=entity.type,
        entity_revision=entity.revision, chunk_id=membership.chunk_id,
        observed_at=membership.observed_at,
        name=entity.name if entity.name_origin is not None else None,
    ) for membership, entity in rows]


async def list_retained_version_membership_refs(
    session: AsyncSession, document_version_id: UUID, chunk_ids: list[UUID],
) -> list[VersionMembershipReference]:
    """Return canonical membership keys for exact currently permitted retained chunks.

    Accepts an older retained version without making it current extraction input.
    The caller holds its source/document egress fences and checks destination
    policy; this read checks active source and current generation using document
    public provenance contracts. Up to100 unique chunks and100 memberships are
    authorized completely, never truncated. Missing/deleted/mismatched evidence
    raises LookupError; malformed or over-bound input raises ValueError. Names
    are identity hints, not model seed proof: get_temporal_node_seeds owns that
    source-local field-value proof. No locks, writes or commits occur here.
    """
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Retained membership chunks must be unique and bounded to100")
    if not chunk_ids:
        return []
    from modules.knowledge.documents import public as documents

    fences = await documents.review_version_fences(session, [document_version_id])
    fence = fences.get(document_version_id)
    if fence is None:
        raise LookupError("Retained membership version is unavailable")
    source = await sources.get_connector_source(session, fence.source_id)
    if source is None or source.status != "active" or source.generation != fence.current_source_generation:
        raise LookupError("Retained membership source policy or generation changed")
    try:
        evidence = await documents.read_evidence_refs(
            session, [(document_version_id, chunk_id) for chunk_id in chunk_ids],
        )
    except ValueError as exc:
        raise LookupError("Retained membership evidence is unavailable") from exc
    if len(evidence) != len(chunk_ids) or any(
        ref.document_id != fence.document_id or ref.source_id != fence.source_id
        for ref in evidence
    ):
        raise LookupError("Retained membership evidence identity changed")
    rows = (await session.execute(
        select(EntityEvidenceMembership, Entity)
        .join(Entity, Entity.id == EntityEvidenceMembership.entity_id)
        .where(
            EntityEvidenceMembership.document_version_id == document_version_id,
            EntityEvidenceMembership.chunk_id.in_(chunk_ids),
            ~EntityEvidenceMembership.entity_id.in_(select(EntityRedirect.old_entity_id)),
        )
        .order_by(EntityEvidenceMembership.id).limit(101)
    )).all()
    if len(rows) > 100:
        raise ValueError("Retained membership context exceeds100 exact supports")
    if any(membership.document_id != fence.document_id or membership.source_id != fence.source_id
           for membership, _ in rows):
        raise LookupError("Retained membership owner provenance changed")
    return [VersionMembershipReference(
        membership_id=membership.id, entity_id=entity.id, entity_type=entity.type,
        entity_revision=entity.revision, chunk_id=membership.chunk_id,
        observed_at=membership.observed_at,
        name=entity.name if entity.name_origin is not None else None,
    ) for membership, entity in rows]


async def list_entity_evidence(
    session: AsyncSession, entity_id: UUID, limit: int = 50, cursor: str | None = None
) -> EntityEvidencePage | None:
    """Return evidence for a canonical entity with document-version provenance."""
    if not 1 <= limit <= 100:
        raise ValueError("Entity evidence page limit must be between 1 and 100")
    try:
        canonical_id = await resolve_canonical_entity_id(session, entity_id)
    except LookupError:
        return None
    statement = select(EntityEvidenceMembership).where(EntityEvidenceMembership.entity_id == canonical_id)
    if cursor:
        extracted_at, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id) > (extracted_at, identifier))
    rows = list((await session.scalars(
        statement.order_by(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return EntityEvidencePage(items=[], next_cursor=None)
    from modules.knowledge.documents import public as documents

    refs = await documents.read_evidence_refs(
        session, list(dict.fromkeys((row.document_version_id, row.chunk_id) for row in rows))
    )
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in refs}
    items = [
        EntityEvidenceRead(
            id=row.id, entity_id=row.entity_id, document_id=ref.document_id,
            document_version_id=row.document_version_id, version_number=ref.version_number,
            chunk_id=row.chunk_id, observed_at=row.observed_at, extracted_at=row.extracted_at,
            confidence=row.confidence, source_id=ref.source_id, title=ref.title,
            canonical_url=ref.canonical_url,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot, excerpt=ref.excerpt,
        )
        for row in rows
        if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
    ]
    next_cursor = encode_cursor(rows[-1].extracted_at, rows[-1].id) if has_more and rows else None
    return EntityEvidencePage(items=items, next_cursor=next_cursor)


def _review_cursor(timestamp: datetime, work_id: UUID, index: int) -> str:
    """Encode a review item position as unpadded URL-safe JSON cursor data."""
    raw = json.dumps([timestamp.isoformat(), str(work_id), index], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_review_cursor(value: str) -> tuple[datetime, UUID, int]:
    """Validate and decode a review cursor, rejecting malformed positions."""
    try:
        if not value or len(value) > 512:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(raw)
        if (not isinstance(decoded, list) or len(decoded) != 3
                or not isinstance(decoded[0], str) or not isinstance(decoded[1], str)
                or type(decoded[2]) is not int):
            raise ValueError
        timestamp, work_id, index = decoded
        result = datetime.fromisoformat(timestamp), UUID(work_id), index
        if result[2] < 0 or result[2] > 100:
            raise ValueError
        return result
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid review cursor") from exc


async def list_review_candidates(session: AsyncSession, limit: int = 50, cursor: str | None = None) -> EntityReviewPage:
    """Bounded owner projection; only immutable future snapshots are actionable."""
    if not 1 <= limit <= 100:
        raise ValueError("Review page limit must be between 1 and 100")
    cursor_time, cursor_work, cursor_index = _decode_review_cursor(cursor) if cursor else (None, None, 0)
    statement = select(EntityExtractionWork, EntityExtractionResult).join(
        EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id
    ).where(EntityExtractionResult.review_json.is_not(None))
    if cursor_time is not None and cursor_work is not None:
        statement = statement.where(tuple_(EntityExtractionWork.updated_at, EntityExtractionWork.id) <= (cursor_time, cursor_work))
    rows = (await session.execute(
        statement
        .order_by(EntityExtractionWork.updated_at.desc(), EntityExtractionWork.id.desc())
        .limit(101)
    )).all()
    from modules.knowledge.documents import public as documents
    fences = await documents.review_version_fences(session, [work.document_version_id for work, _ in rows[:100]])
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
            if isinstance(state, dict) and state.get("kind") in {"assigned", "resolved", "suppressed"}:
                continue
            name, reason = candidate.get("candidate_name", candidate.get("candidate_name")), candidate.get("reason")
            if not isinstance(name, str) or not isinstance(reason, str):
                continue
            ids = candidate.get("possible_entity_ids", [])
            valid_ids = []
            for value in ids if isinstance(ids, list) else []:
                try:
                    valid_ids.append(UUID(str(value)))
                except ValueError:
                    continue
            kind = candidate.get("kind") if candidate.get("kind") in {"entity", "relationship"} else (
                "relationship" if reason.startswith("relationship_") else "entity"
            )
            candidate_id = None
            try:
                candidate_id = UUID(str(candidate.get("candidate_id"))) if candidate.get("candidate_id") else None
            except ValueError:
                pass
            digest_ok = False
            try:
                digest_ok = candidate.get("snapshot_digest") == _review_snapshot_digest(candidate)
            except (KeyError, TypeError):
                pass
            actionable = bool(candidate_id and digest_ok and not state and work.status == "succeeded" and (
                work.document_version_id in fences and (
                kind == "entity" and candidate.get("candidate_key") and candidate.get("match_fingerprint") and candidate.get("chunk_ids")
                or kind == "relationship" and candidate.get("source_candidate_key") and candidate.get("target_candidate_key") and candidate.get("chunk_id") and candidate.get("relationship_type")
                )
            ))
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
            items.append(EntityReviewCandidate(
                kind=kind, candidate_id=candidate_id, work_id=work.id, result_id=result.id,
                snapshot_digest=candidate.get("snapshot_digest") if actionable else None,
                document_version_id=work.document_version_id, source_generation=work.source_generation,
                owner_generation=fence.current_source_generation if fence else None,
                document_id=fence.document_id if fence else None,
                version_number=fence.version_number if fence else None,
                source_id=fence.source_id if fence else None,
                source_name=fence.source_name if fence else None,
                actionable=actionable, status=work.status, candidate_name=name[:300],
                candidate_type=str(candidate.get("candidate_type")) if candidate.get("candidate_type") else None,
                reason=reason[:128], possible_entity_ids=valid_ids[:30],
                relationship_type=str(candidate.get("relationship_type")) if kind == "relationship" and candidate.get("relationship_type") else None,
            ))
            evidence_keys.append(candidate_evidence)
            endpoint_keys.append((work.id, work.document_version_id, str(candidate.get("source_candidate_key")), str(candidate.get("target_candidate_key")), candidate_evidence[0][1]) if kind == "relationship" and candidate.get("source_candidate_key") and candidate.get("target_candidate_key") and candidate_evidence else None)
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
    all_keys = list(dict.fromkeys(key for group in evidence_keys for key in group))
    refs_by_pair = {}
    for offset in range(0, len(all_keys), 100):
        refs = await documents.read_evidence_refs(session, all_keys[offset:offset + 100])
        refs_by_pair.update({(ref.document_version_id, ref.chunk_id): ref for ref in refs})
    endpoint_bindings = list(dict.fromkeys(
        (str(work_id), key, version_id, chunk_id)
        for endpoints in endpoint_keys if endpoints is not None
        for work_id, version_id, source_key, target_key, chunk_id in (endpoints,)
        for key in (source_key, target_key)
    ))
    endpoint_rows = list((await session.scalars(
        select(EntityEvidenceMembership).where(
            tuple_(EntityEvidenceMembership.extraction_identity, EntityEvidenceMembership.candidate_key,
                   EntityEvidenceMembership.document_version_id, EntityEvidenceMembership.chunk_id).in_(endpoint_bindings)
        ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(401)
    )).all()) if endpoint_bindings else []
    endpoint_entities = sorted({row.entity_id for row in endpoint_rows}, key=str)
    entity_refs = []
    for offset in range(0, len(endpoint_entities), 100):
        entity_refs.extend(await get_entity_refs(session, endpoint_entities[offset:offset + 100]))
    entity_refs_by_id = {ref.requested_id: ref for ref in entity_refs}
    for item, keys, endpoints in zip(items, evidence_keys, endpoint_keys):
        fence = fences.get(item.document_version_id)
        item.evidence = [EntityReviewEvidence(
            document_id=ref.document_id, document_version_id=ref.document_version_id,
            version_number=ref.version_number, chunk_id=ref.chunk_id, source_id=ref.source_id,
            source_name=fence.source_name if fence else "", title=ref.title,
            canonical_url=ref.canonical_url,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
            observed_at=ref.observed_at, excerpt=ref.excerpt,
        ) for key in keys if (ref := refs_by_pair.get(key)) is not None]
        if endpoints is None:
            continue
        work_id, version_id, source_key, target_key, chunk_id = endpoints
        endpoint_dtos = []
        for key in (source_key, target_key):
            matches = [row for row in endpoint_rows if row.extraction_identity == str(work_id) and row.candidate_key == key and row.document_version_id == version_id and row.chunk_id == chunk_id]
            if len(matches) != 1:
                endpoint_dtos.append(EntityReviewEndpoint(state="ambiguous" if matches else "unassigned"))
                continue
            member = matches[0]
            ref = entity_refs_by_id.get(member.entity_id)
            if ref is None:
                endpoint_dtos.append(EntityReviewEndpoint(state="unassigned"))
                continue
            endpoint_dtos.append(EntityReviewEndpoint(
                state="assigned", entity_id=ref.canonical_id, entity_name=ref.name,
                entity_type=ref.type, membership_id=member.id,
            ))
        item.source_endpoint, item.target_endpoint = endpoint_dtos
        if (len(endpoint_dtos) != 2 or any(endpoint.state != "assigned" for endpoint in endpoint_dtos)
                or endpoint_dtos[0].entity_id == endpoint_dtos[1].entity_id):
            item.actionable = False
            item.snapshot_digest = None
    return EntityReviewPage(items=items, next_cursor=next_cursor)


def _review_snapshot_digest(candidate: dict[str, object]) -> str:
    """Hash the immutable identity fields used to fence owner review actions."""
    keys = (
        ("kind", "candidate_id", "candidate_type", "candidate_name", "candidate_key", "match_fingerprint", "chunk_ids", "confidence")
        if candidate.get("kind") == "entity" else
        ("kind", "candidate_id", "candidate_name", "relationship_type", "source_candidate_key", "target_candidate_key", "chunk_id", "confidence")
    )
    snapshot = {key: candidate[key] for key in keys}
    return sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _review_entity_bindings(review: list[object]) -> dict[str, tuple[str, set[UUID]]]:
    """Validate entity review selectors and return bounded fingerprint/chunk bindings."""
    bindings: dict[str, tuple[str, set[UUID]]] = {}
    for item in review:
        if not isinstance(item, dict) or item.get("kind") != "entity":
            continue
        fingerprint, raw_chunks = item.get("match_fingerprint"), item.get("chunk_ids")
        if fingerprint is None and raw_chunks is None:
            continue
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
                or not isinstance(raw_chunks, list) or not 1 <= len(raw_chunks) <= 5
                or not isinstance(item.get("candidate_key"), str) or len(item["candidate_key"]) != 64
                or not item.get("candidate_id")):
            raise ValueError("Same-result candidate selectors are incomplete; reload the review")
        try:
            chunks = {UUID(str(value)) for value in raw_chunks}
            key = str(UUID(str(item["candidate_id"])))
            if item.get("snapshot_digest") != _review_snapshot_digest(item):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Same-result candidate selectors are invalid; reload the review") from exc
        if len(chunks) != len(raw_chunks) or key in bindings:
            raise ValueError("Same-result candidate selectors are ambiguous; reload the review")
        bindings[key] = (fingerprint, chunks)
    if len(bindings) > 30:
        raise ValueError("Same-result candidate selector set exceeds its limit")
    return bindings


async def assign_review_candidate(
    session: AsyncSession, candidate_id: UUID, payload: EntityReviewAssignmentRequest, *, actor_id: int,
) -> EntityReviewAssignmentResult:
    """Bind one durable extraction candidate to an owner-selected canonical entity."""
    from modules.knowledge.documents import public as documents

    result_hint = await session.get(EntityExtractionResult, payload.result_id)
    if result_hint is None:
        raise LookupError("Review result is unavailable")
    work_hint = await session.get(EntityExtractionWork, result_hint.work_id)
    if work_hint is None:
        raise LookupError("Review work is unavailable")
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    reviews = result_hint.review_json if isinstance(result_hint.review_json, list) else []
    candidates = [item for item in reviews if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)]
    if len(candidates) != 1:
        raise LookupError("Review candidate is no longer available")
    snapshot = candidates[0]
    if snapshot.get("kind") != "entity" or snapshot.get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(snapshot) != payload.snapshot_digest:
        raise ValueError("Review candidate snapshot changed; reload the queue")
    if snapshot.get("decision_state"):
        raise ValueError("Review candidate was already resolved")
    if payload.future_document_id is not None:
        raise ValueError("Future document scope is not available from this review action")
    try:
        candidate_type = str(snapshot["candidate_type"])
        candidate_key = str(snapshot["candidate_key"])
        fingerprint = str(snapshot["match_fingerprint"])
        chunks = [UUID(str(value)) for value in snapshot["chunk_ids"]]
        confidence = float(snapshot["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Review candidate evidence selector is unavailable") from exc
    if not 1 <= len(chunks) <= 5 or len(set(chunks)) != len(chunks) or len(fingerprint) != 64 or len(candidate_key) != 64:
        raise ValueError("Review candidate evidence selector is invalid")

    locator = await documents.review_version_locator(session, work_hint.document_version_id)
    if locator is None:
        raise LookupError("Review evidence was removed")
    document_id, source_id = locator
    source = await sources.lock_source(session, source_id)
    if source is None or source.generation != payload.expected_owner_generation:
        raise ValueError("Owner evidence fence changed; reload the review candidate")
    evidence = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work_hint.document_version_id, source_generation=source.generation,
        chunk_ids=chunks,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")

    bindings = _review_entity_bindings(reviews)
    if str(candidate_id) not in bindings or bindings[str(candidate_id)] != (fingerprint, set(chunks)):
        raise ValueError("Selected candidate is missing from the complete result selector set")
    prior = await get_document_correction_decisions(session, document_id, work_hint.document_version_id, bindings)
    prior_decision = prior.get(str(candidate_id))
    if prior_decision is not None and (prior_decision[1] != "assign" or prior_decision[2] != payload.target_entity_id):
        raise ValueError("A conflicting correction decision already exists")
    refs = await get_entity_refs(session, [payload.target_entity_id], for_write=True)
    target = refs[0]
    if target.canonical_id != payload.target_entity_id or target.type != candidate_type or target.revision != payload.expected_target_revision:
        raise ValueError("Target entity changed or has an incompatible identity")

    work = await session.scalar(select(EntityExtractionWork).where(EntityExtractionWork.id == work_hint.id).with_for_update().execution_options(populate_existing=True))
    result = await session.scalar(select(EntityExtractionResult).where(
        EntityExtractionResult.id == payload.result_id, EntityExtractionResult.work_id == work_hint.id,
    ).with_for_update().execution_options(populate_existing=True))
    if work is None or result is None or work.status != "succeeded" or work.document_version_id != extraction_version_id or work.source_generation != extraction_generation or work.id != extraction_work_id:
        raise ValueError("Review extraction changed; reload the queue")
    current_snapshot = [item for item in (result.review_json if isinstance(result.review_json, list) else []) if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)]
    if len(current_snapshot) != 1 or current_snapshot[0].get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(current_snapshot[0]) != payload.snapshot_digest or current_snapshot[0].get("decision_state"):
        raise ValueError("Review candidate snapshot changed or was resolved")
    evidence = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work.document_version_id, source_generation=payload.expected_owner_generation,
        chunk_ids=chunks,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")
    current_bindings = _review_entity_bindings(result.review_json if isinstance(result.review_json, list) else [])
    if current_bindings != bindings:
        raise ValueError("Same-result candidate selectors changed; reload the review")
    current = await get_document_correction_decisions(session, document_id, work.document_version_id, current_bindings, for_update=True)
    decision = current.get(str(candidate_id))
    if decision != prior_decision:
        raise ValueError("Correction decision changed; reload the review")

    memberships = [await record_extraction_membership(
        session, entity_id=payload.target_entity_id,
        evidence_ref=documents.ExtractionEvidenceRef(
            document_id=ref.document_id, document_version_id=ref.document_version_id,
            source_id=ref.source_id, source_generation=ref.current_source_generation,
            chunk_id=ref.chunk_id,
        ),
        source_generation=payload.expected_owner_generation, extraction_identity=str(work.id),
        candidate_key=candidate_key, match_fingerprint=fingerprint,
        observed_at=ref.observed_at, confidence=confidence,
    ) for ref in evidence]
    for membership_id in memberships:
        existing = await session.scalar(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "evidence", EntityCorrectionDecision.membership_id == membership_id,
        ).with_for_update())
        if existing is None:
            session.add(EntityCorrectionDecision(
                decision="assign", scope="evidence", entity_id=payload.target_entity_id,
                membership_id=membership_id, match_fingerprint=fingerprint, actor_id=actor_id,
                reason=payload.reason, created_at=datetime.now(UTC),
            ))
        elif existing.decision != "assign" or existing.entity_id != payload.target_entity_id:
            raise ValueError("Evidence membership already has a conflicting owner decision")
    updated_review = deepcopy(result.review_json)
    for item in updated_review:
        if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id):
            item["decision_state"] = {"kind": "assigned", "target_entity_id": str(payload.target_entity_id), "membership_ids": [str(value) for value in memberships]}
            break
    result.review_json = updated_review
    await record_owner_action(
        session, actor_id=actor_id, operation="entity_review_assign", reason=payload.reason,
        affected_ids=[payload.target_entity_id, candidate_id, *memberships],
        revisions={str(payload.target_entity_id): target.revision},
    )
    await session.flush()
    await commit_with_replay(session, [make_graph_change(entity_id=payload.target_entity_id)])
    return EntityReviewAssignmentResult(candidate_id=candidate_id, target_entity_id=payload.target_entity_id, membership_ids=memberships, revision=target.revision)


async def resolve_relationship_review(
    session: AsyncSession, candidate_id: UUID, payload: EntityRelationshipReviewRequest, *, actor_id: int,
) -> EntityRelationshipReviewResult:
    """Publish a stored relationship only after both exact endpoint memberships exist."""
    from modules.knowledge.documents import public as documents
    from modules.knowledge.relationships import public as relationships

    result_hint = await session.get(EntityExtractionResult, payload.result_id)
    if result_hint is None:
        raise LookupError("Review result is unavailable")
    work_hint = await session.get(EntityExtractionWork, result_hint.work_id)
    if work_hint is None:
        raise LookupError("Review work is unavailable")
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    reviews = result_hint.review_json if isinstance(result_hint.review_json, list) else []
    snapshots = [item for item in reviews if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)]
    if len(snapshots) != 1 or snapshots[0].get("kind") != "relationship":
        raise LookupError("Relationship review snapshot is unavailable")
    snapshot = snapshots[0]
    if snapshot.get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(snapshot) != payload.snapshot_digest:
        raise ValueError("Relationship review snapshot changed")
    if snapshot.get("decision_state"):
        raise ValueError("Relationship review item was already resolved")
    source_key = str(snapshot.get("source_candidate_key", ""))
    target_key = str(snapshot.get("target_candidate_key", ""))
    relationship_type = str(snapshot.get("relationship_type", ""))
    chunk_id = UUID(str(snapshot.get("chunk_id")))
    if not source_key or not target_key or source_key == target_key or not relationship_type:
        raise ValueError("Relationship endpoint selector is invalid")

    locator = await documents.review_version_locator(session, work_hint.document_version_id)
    if locator is None:
        raise LookupError("Relationship evidence was removed")
    document_id, source_id = locator
    source = await sources.lock_source(session, source_id)
    if source is None or source.generation != payload.expected_owner_generation:
        raise ValueError("Owner evidence fence changed; reload the relationship review")
    refs = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work_hint.document_version_id, source_generation=source.generation,
        chunk_ids=[chunk_id],
    )
    if refs is None:
        raise LookupError("Relationship evidence is no longer retained")

    async def endpoint_memberships() -> tuple[EntityEvidenceMembership, EntityEvidenceMembership]:
        """Require one distinct endpoint membership per candidate on the cited chunk."""
        rows = list((await session.scalars(select(EntityEvidenceMembership).where(
            EntityEvidenceMembership.extraction_identity == str(work_hint.id),
            EntityEvidenceMembership.candidate_key.in_([source_key, target_key]),
            EntityEvidenceMembership.document_version_id == work_hint.document_version_id,
            EntityEvidenceMembership.chunk_id == chunk_id,
        ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(3))).all())
        left = [row for row in rows if row.candidate_key == source_key]
        right = [row for row in rows if row.candidate_key == target_key]
        if len(left) != 1 or len(right) != 1 or left[0].id == right[0].id or left[0].entity_id == right[0].entity_id:
            raise ValueError("Both endpoints require one distinct exact evidence membership on the cited chunk")
        return left[0], right[0]

    source_membership, target_membership = await endpoint_memberships()
    expected_membership_ids = (source_membership.id, target_membership.id)
    expected_membership_identity = {
        source_membership.id: (source_membership.entity_id, source_membership.document_version_id, source_membership.chunk_id),
        target_membership.id: (target_membership.entity_id, target_membership.document_version_id, target_membership.chunk_id),
    }
    endpoint_refs = await get_entity_refs(session, [source_membership.entity_id, target_membership.entity_id], for_write=True)
    if len(endpoint_refs) != 2 or any(ref.requested_id != ref.canonical_id for ref in endpoint_refs):
        raise ValueError("Relationship endpoint identity changed; review the assignments")
    locked = await get_membership_refs(session, [source_membership.id, target_membership.id], for_write=True)
    work = await session.scalar(select(EntityExtractionWork).where(EntityExtractionWork.id == work_hint.id).with_for_update().execution_options(populate_existing=True))
    result = await session.scalar(select(EntityExtractionResult).where(
        EntityExtractionResult.id == payload.result_id, EntityExtractionResult.work_id == work_hint.id,
    ).with_for_update().execution_options(populate_existing=True))
    if work is None or result is None or work.status != "succeeded" or work.source_generation != extraction_generation or work.document_version_id != extraction_version_id or work.id != extraction_work_id:
        raise ValueError("Relationship extraction changed; reload the queue")
    current = [item for item in (result.review_json if isinstance(result.review_json, list) else []) if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)]
    if len(current) != 1 or current[0].get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(current[0]) != payload.snapshot_digest or current[0].get("decision_state"):
        raise ValueError("Relationship review snapshot changed or was resolved")
    source_membership, target_membership = await endpoint_memberships()
    locked_by_id = {item.id: (item.entity_id, item.document_version_id, item.chunk_id) for item in locked}
    current_membership_ids = (source_membership.id, target_membership.id)
    if (current_membership_ids != expected_membership_ids
            or locked_by_id != expected_membership_identity
            or any(item.entity_id not in {ref.canonical_id for ref in endpoint_refs} for item in locked)):
        raise ValueError("Endpoint membership identity changed")
    relationship_id = await relationships.publish_extracted_relationship(
        session, source_entity_id=source_membership.entity_id,
        target_entity_id=target_membership.entity_id, relationship_type=relationship_type,
        document_version_id=work.document_version_id, chunk_id=chunk_id,
        source_membership_id=source_membership.id, target_membership_id=target_membership.id,
        confidence=float(snapshot["confidence"]),
    )
    if relationship_id is None:
        raise ValueError("Relationship endpoints are identical")
    updated_review = deepcopy(result.review_json)
    for item in updated_review:
        if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id):
            item["decision_state"] = {"kind": "resolved", "relationship_id": str(relationship_id)}
            break
    result.review_json = updated_review
    await record_owner_action(
        session, actor_id=actor_id, operation="relationship_review_resolve", reason=payload.reason,
        affected_ids=[relationship_id, candidate_id, source_membership.id, target_membership.id],
    )
    await session.flush()
    await commit_with_replay(session, [make_graph_change(relationship_id=relationship_id)])
    return EntityRelationshipReviewResult(candidate_id=candidate_id, relationship_id=relationship_id)


async def publish_derived_field(
    session: AsyncSession,
    *,
    entity_id: UUID,
    membership_id: UUID,
    field_name: Literal["name", "description"],
    value: str,
) -> bool:
    """Publish a derived field and bind its exact value to one valid membership.

    The caller owns the source/document locks and the outer transaction. This
    command takes entity then membership locks and queues temporal desired state
    with exact current support in the same transaction; it never commits.
    """
    if field_name not in {"name", "description"}:
        raise ValueError("Unsupported derived entity field")
    entity = await session.scalar(
        select(Entity).where(Entity.id == entity_id).with_for_update()
    )
    if entity is None:
        raise LookupError("Entity is missing")
    membership = await session.scalar(
        select(EntityEvidenceMembership)
        .where(
            EntityEvidenceMembership.id == membership_id,
            EntityEvidenceMembership.entity_id == entity_id,
        )
        .with_for_update()
    )
    if membership is None:
        raise LookupError("Entity evidence membership is missing or belongs to another entity")
    if (field_name == "name" and entity.name_origin == "owner") or (
        field_name == "description" and entity.description_origin == "owner"
    ):
        return False
    previous_value = entity.name if field_name == "name" else entity.description
    previous_origin = entity.name_origin if field_name == "name" else entity.description_origin
    if field_name == "name":
        value = " ".join(value.split())
        if not value or len(value) > 300:
            raise ValueError("Derived entity name must contain 1 to 300 characters")
        entity.name = value
        entity.canonical_name = canonicalize_name(value)
        entity.name_origin = "derived"
    else:
        if not value or len(value) > 20_000:
            raise ValueError("Derived entity description must contain 1 to 20000 characters")
        entity.description = value
        entity.description_origin = "derived"
    value_hash = sha256(value.encode("utf-8")).hexdigest()
    await session.execute(delete(EntityFieldEvidence).where(
        EntityFieldEvidence.entity_id == entity_id,
        EntityFieldEvidence.field_name == field_name,
        EntityFieldEvidence.value_hash != value_hash,
    ))
    support_exists = await session.scalar(select(EntityFieldEvidence.id).where(
        EntityFieldEvidence.entity_id == entity_id,
        EntityFieldEvidence.field_name == field_name,
        EntityFieldEvidence.value_hash == value_hash,
        EntityFieldEvidence.membership_id == membership_id,
    ).limit(1))
    if support_exists is None:
        session.add(EntityFieldEvidence(
            entity_id=entity_id,
            field_name=field_name,
            value_hash=value_hash,
            membership_id=membership_id,
        ))
        entity.revision += 1
    elif previous_value != value or previous_origin != "derived":
        entity.revision += 1
    await session.flush()
    if support_exists is None or previous_value != value or previous_origin != "derived":
        await _schedule_entity_change(session, entity, [field_name, "support"], "derived")
    return True


async def schedule_extraction_work(
    session: AsyncSession, document_version_id: UUID, source_generation: int,
    extractor_version: str, prompt_version: str,
) -> EntityExtractionWork:
    """Create or lock durable extraction work keyed by version and prompt identity."""
    if len(extractor_version) > 64 or len(prompt_version) > 64:
        raise ValueError("Extraction versions are too long")
    await session.execute(pg_insert(EntityExtractionWork).values(
        document_version_id=document_version_id,
        source_generation=source_generation,
        extractor_version=extractor_version,
        prompt_version=prompt_version,
    ).on_conflict_do_nothing(constraint="uq_entity_extraction_work_identity"))
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.document_version_id == document_version_id,
        EntityExtractionWork.extractor_version == extractor_version,
        EntityExtractionWork.prompt_version == prompt_version,
    ).with_for_update())
    if work is None:
        raise RuntimeError("Extraction work could not be scheduled")
    # A durable work identity captures source generation once. Duplicate ready
    # events and recovery cannot rebind it to a later source generation.
    return work


async def claim_extraction_work(
    session: AsyncSession, work_id: UUID, lease_owner: str, now: datetime
) -> EntityExtractionWork | None:
    """Claim due work under a lease, enforcing expiry and the five-attempt ceiling."""
    work = await session.scalar(select(EntityExtractionWork).where(EntityExtractionWork.id == work_id).with_for_update())
    if work is None or work.status not in {"pending", "running"}:
        return None
    if work.status == "running" and work.lease_expires_at is not None and work.lease_expires_at > now:
        return None
    if work.attempt >= 5:
        work.status = "failed"
        work.error_code = "attempt_limit_exhausted"
        work.lease_owner = None
        work.lease_expires_at = None
        await session.flush()
        return None
    if work.next_attempt_at > now:
        return None
    work.attempt += 1
    work.status = "running"
    work.lease_owner = lease_owner
    work.lease_expires_at = now + timedelta(seconds=110)
    work.error_code = None
    await session.flush()
    return work


async def list_recoverable_extraction_work(session: AsyncSession, limit: int = 25) -> list[UUID]:
    """Lock and return bounded pending or expired extraction work IDs."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction recovery limit must be between 1 and 100")
    now = datetime.now(UTC)
    return list((await session.scalars(
        select(EntityExtractionWork.id).where(
            EntityExtractionWork.next_attempt_at <= now,
            or_(
                EntityExtractionWork.status == "pending",
                (EntityExtractionWork.status == "running") & (EntityExtractionWork.lease_expires_at <= now),
            ),
        ).order_by(EntityExtractionWork.next_attempt_at, EntityExtractionWork.created_at)
        .limit(limit).with_for_update(skip_locked=True)
    )).all())


async def terminalize_exhausted_extraction_work(
    session: AsyncSession, limit: int = 25
) -> int:
    """Fail expired running work at the attempt ceiling and clear its lease."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction terminalization limit must be between 1 and 100")
    now = datetime.now(UTC)
    rows = list((await session.scalars(
        select(EntityExtractionWork).where(
            EntityExtractionWork.status == "running",
            EntityExtractionWork.attempt >= 5,
            EntityExtractionWork.lease_expires_at <= now,
        ).order_by(EntityExtractionWork.lease_expires_at, EntityExtractionWork.id)
        .limit(limit).with_for_update(skip_locked=True)
    )).all())
    for work in rows:
        work.status = "failed"
        work.error_code = "attempt_limit_exhausted"
        work.lease_owner = None
        work.lease_expires_at = None
    return len(rows)


async def list_blocked_extraction_work(session: AsyncSession, limit: int = 25) -> list[tuple[UUID, UUID, int, str | None, str | None]]:
    """List due policy/capability-blocked work with its dependency fingerprint."""
    if not 1 <= limit <= 100:
        raise ValueError("Blocked extraction page size must be between 1 and 100")
    rows = (await session.execute(
        select(
            EntityExtractionWork.id, EntityExtractionWork.document_version_id,
            EntityExtractionWork.source_generation, EntityExtractionWork.error_code,
            EntityExtractionWork.dependency_fingerprint,
        ).where(
            EntityExtractionWork.status == "blocked",
            EntityExtractionWork.error_code.in_(("ai_policy_denied", "structured_unsupported")),
            EntityExtractionWork.next_attempt_at <= datetime.now(UTC),
        ).order_by(EntityExtractionWork.updated_at, EntityExtractionWork.id).limit(limit)
    )).all()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


async def requeue_blocked_extraction_work(
    session: AsyncSession, work_id: UUID, previous_fingerprint: str | None, current_fingerprint: str
) -> bool:
    """Requeue blocked work only after its dependency fingerprint has changed."""
    if previous_fingerprint is None or previous_fingerprint == current_fingerprint:
        return False
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.status == "blocked",
        EntityExtractionWork.dependency_fingerprint == previous_fingerprint,
    ).with_for_update())
    if work is None:
        return False
    if work.lease_owner is not None and work.lease_expires_at is not None and work.lease_expires_at > datetime.now(UTC):
        return False
    work.status = "pending"
    work.attempt = 0
    work.next_attempt_at = datetime.now(UTC)
    work.error_code = None
    work.dependency_fingerprint = None
    work.lease_owner = None
    work.lease_expires_at = None
    return True


async def defer_blocked_extraction_recheck(
    session: AsyncSession, work_id: UUID, fingerprint: str, *, minutes: int = 15
) -> None:
    """Delay a matching blocked work item's next dependency recheck."""
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.status == "blocked",
        EntityExtractionWork.dependency_fingerprint == fingerprint,
    ).with_for_update())
    if work is not None:
        work.next_attempt_at = datetime.now(UTC) + timedelta(minutes=minutes)


async def get_extraction_status(session: AsyncSession, document_version_id: UUID):
    """Return the newest work status and stored facts for one document version."""
    from modules.knowledge.entities.schemas import EntityExtractionStatus

    row = (await session.execute(
        select(EntityExtractionWork, EntityExtractionResult)
        .outerjoin(EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id)
        .where(EntityExtractionWork.document_version_id == document_version_id)
        .order_by(desc(EntityExtractionWork.created_at)).limit(1)
    )).one_or_none()
    if row is None:
        return None
    work, result = row
    return EntityExtractionStatus(
        document_version_id=work.document_version_id, status=work.status, attempt=work.attempt,
        error_code=work.error_code, model=result.model if result else None,
        facts=result.facts_json if result else [], review_candidates=result.review_json if result else [],
        completed_at=result.completed_at if result else None,
    )


async def finish_extraction_work(
    session: AsyncSession, work_id: UUID, lease_owner: str, *, facts: list[dict[str, object]],
    review: list[dict[str, object]], model: str | None, usage: dict[str, object] | None,
) -> bool:
    """Persist results and succeed only while the caller still owns a live lease."""
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.lease_owner == lease_owner,
        EntityExtractionWork.lease_expires_at > datetime.now(UTC),
    ).with_for_update())
    if work is None:
        return False
    result = await session.scalar(select(EntityExtractionResult).where(EntityExtractionResult.work_id == work.id).with_for_update())
    if result is None:
        session.add(EntityExtractionResult(
            work_id=work.id, model=model, usage_json=usage, facts_json=facts, review_json=review,
        ))
    else:
        result.model, result.usage_json, result.facts_json, result.review_json = model, usage, facts, review
    work.status = "succeeded"
    work.lease_owner = None
    work.lease_expires_at = None
    await session.flush()
    return True


async def set_extraction_work_error(
    session: AsyncSession, work_id: UUID, lease_owner: str, error_code: str, *,
    blocked: bool = False, dependency_fingerprint: str | None = None,
) -> None:
    """Mutate failure state only while the caller still owns a live lease.

    Changes the ORM row in the caller's transaction without explicitly flushing
    or committing; callers commit the session. Returns without mutation when
    the live lease is absent or owned by someone else. Retry delay is exponential
    in minutes up to 60; blocked work normally waits 15 minutes. Dependency
    recovery considers only ``ai_policy_denied`` and ``structured_unsupported``
    rows with a fingerprint. Local-only work has no fingerprint and is parked at
    ``datetime.max`` rather than requeued after a dependency change.
    """
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.lease_owner == lease_owner,
        EntityExtractionWork.lease_expires_at > datetime.now(UTC),
    ).with_for_update())
    if work is None:
        return
    work.status = "blocked" if blocked else ("failed" if work.attempt >= 5 else "pending")
    work.error_code = error_code[:64]
    work.dependency_fingerprint = dependency_fingerprint if blocked else None
    work.next_attempt_at = (
        datetime.max.replace(tzinfo=UTC) if error_code == "local_only_source"
        else datetime.now(UTC) + timedelta(minutes=15 if blocked else min(2 ** work.attempt, 60))
    )
    work.lease_owner = None
    work.lease_expires_at = None


async def list_resolution_candidates(
    session: AsyncSession, entity_type: str, candidate_names: list[str], limit: int = 1000
) -> tuple[list[dict[str, object]], bool]:
    """Return bounded same-type entities and exact confirmed aliases for resolution."""
    if not 1 <= limit <= 1000 or not 1 <= len(candidate_names) <= 30:
        raise ValueError("Resolution context must be bounded")
    normalized_names = sorted({canonicalize_name(name) for name in candidate_names})
    rows = list((await session.execute(
        select(Entity.id, Entity.type, Entity.name, Entity.revision)
        .where(
            Entity.type == entity_type,
            ~Entity.id.in_(select(EntityRedirect.old_entity_id)),
        ).order_by(Entity.id).limit(limit + 1)
    )).all())
    overflow = len(rows) > limit
    rows = rows[:limit]
    aliases = (await session.execute(
        select(EntityAlias.entity_id, EntityAlias.normalized_alias)
        .where(
            EntityAlias.entity_id.in_([row.id for row in rows]),
            EntityAlias.confirmed.is_(True),
            EntityAlias.normalized_alias.in_(normalized_names),
        )
        .order_by(EntityAlias.entity_id, EntityAlias.normalized_alias).limit(limit + 1)
    )).all() if rows and not overflow else []
    overflow = overflow or len(aliases) > limit
    aliases = aliases[:limit]
    alias_map: dict[UUID, list[str]] = {}
    for entity_id, alias in aliases:
        alias_map.setdefault(entity_id, []).append(alias)
    return ([
        {"id": row.id, "type": row.type, "name": row.name,
         "revision": row.revision, "confirmed_aliases": alias_map.get(row.id, [])}
        for row in rows
    ], overflow)


async def create_extracted_entity(session: AsyncSession, entity_type: str) -> UUID:
    """Create an unnamed derived entity for later evidence-backed field publication."""
    entity = Entity(type=entity_type, name=None, canonical_name=None, name_origin=None, description_origin=None)
    session.add(entity)
    await session.flush()
    return entity.id


async def find_extraction_entity(
    session: AsyncSession, *, extraction_identity: str, candidate_key: str
) -> UUID | None:
    """Return the canonical entity already bound to a deterministic extraction key, if any.

    Deterministic mappers (for example GitHub) use this with
    ``record_extraction_membership`` to stay idempotent: the first membership
    creates the entity, later calls find it. The earliest membership wins so
    the answer is stable; a merged entity is followed to its canonical ID. The
    caller holds the source lock that serializes find-or-create.
    """
    entity_id = await session.scalar(
        select(EntityEvidenceMembership.entity_id).where(
            EntityEvidenceMembership.extraction_identity == extraction_identity,
            EntityEvidenceMembership.candidate_key == candidate_key,
        ).order_by(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id).limit(1)
    )
    if entity_id is None:
        return None
    try:
        return await resolve_canonical_entity_id(session, entity_id)
    except LookupError:
        # The owner deleted the entity: report "absent" so the mapper recreates it
        # deterministically instead of failing every later record of the source.
        return None


async def record_extraction_membership(
    session: AsyncSession, *, entity_id: UUID, evidence_ref: ExtractionEvidenceRef,
    source_generation: int, extraction_identity: str, candidate_key: str,
    match_fingerprint: str, observed_at: datetime, confidence: float,
) -> UUID:
    """Insert idempotent evidence membership after validating source generation and identity."""
    if (
        not extraction_identity or len(extraction_identity) > 256
        or not candidate_key or len(candidate_key) > 256
        or len(match_fingerprint) != 64
        or not math.isfinite(confidence) or not 0 <= confidence <= 1
        or evidence_ref.source_generation != source_generation
    ):
        raise ValueError("Extraction membership values are invalid")
    entity = await session.scalar(select(Entity).where(Entity.id == entity_id).with_for_update())
    if entity is None:
        raise LookupError("Extraction entity is missing")
    await session.execute(pg_insert(EntityEvidenceMembership).values(
        entity_id=entity_id, document_id=evidence_ref.document_id, source_id=evidence_ref.source_id,
        document_version_id=evidence_ref.document_version_id, chunk_id=evidence_ref.chunk_id,
        extraction_identity=extraction_identity, candidate_key=candidate_key,
        match_fingerprint=match_fingerprint,
        observed_at=observed_at, confidence=confidence,
    ).on_conflict_do_nothing(constraint="uq_entity_evidence_retry"))
    membership_id = await session.scalar(select(EntityEvidenceMembership.id).where(
        EntityEvidenceMembership.extraction_identity == extraction_identity,
        EntityEvidenceMembership.candidate_key == candidate_key,
        EntityEvidenceMembership.chunk_id == evidence_ref.chunk_id,
    ))
    if membership_id is None:
        raise RuntimeError("Entity evidence membership could not be recorded")
    member = await session.scalar(select(EntityEvidenceMembership).where(EntityEvidenceMembership.id == membership_id))
    if member is None or member.entity_id != entity_id:
        raise ValueError("Extraction retry identity resolved to a different entity")
    return membership_id


async def get_document_correction_decisions(
    session: AsyncSession, document_id: UUID, document_version_id: UUID,
    candidates: dict[str, tuple[str, set[UUID]]],
    *, for_update: bool = False,
) -> dict[str, tuple[UUID, str, UUID | None] | None]:
    """Resolve bounded evidence/document owner decisions, reporting ambiguous conflicts."""
    if len(candidates) > 30:
        raise ValueError("Correction decision lookup exceeds its candidate limit")
    if not candidates:
        return {}
    fingerprints = {spec[0] for spec in candidates.values()}
    chunk_ids = {chunk_id for _, chunks in candidates.values() for chunk_id in chunks}
    if len(chunk_ids) > 200 or any(not chunks for _, chunks in candidates.values()):
        raise ValueError("Correction evidence binding exceeds its chunk limit")
    membership_query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.document_id == document_id,
        EntityEvidenceMembership.document_version_id == document_version_id,
        EntityEvidenceMembership.match_fingerprint.in_(fingerprints),
        EntityEvidenceMembership.chunk_id.in_(chunk_ids),
    ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(201)
    if for_update:
        membership_query = membership_query.with_for_update()
    memberships = list((await session.scalars(
        membership_query.execution_options(populate_existing=True)
    )).all())
    memberships_by_selector: dict[tuple[str, UUID], list[EntityEvidenceMembership]] = {}
    for membership in memberships:
        if membership.match_fingerprint is not None:
            memberships_by_selector.setdefault(
                (membership.match_fingerprint, membership.chunk_id), []
            ).append(membership)
    membership_ids = sorted({row.id for row in memberships}, key=str)
    decisions_query = select(EntityCorrectionDecision).where(
        EntityCorrectionDecision.scope == "evidence",
        EntityCorrectionDecision.document_id.is_(None),
        EntityCorrectionDecision.membership_id.in_(membership_ids),
    ).order_by(EntityCorrectionDecision.id).limit(201)
    if for_update:
        decisions_query = decisions_query.with_for_update()
    evidence_decisions = list((await session.scalars(
        decisions_query.execution_options(populate_existing=True)
    )).all()) if membership_ids else []
    membership_by_id = {row.id: row for row in memberships}
    evidence_rows = [
        (row, membership_by_id[row.membership_id].chunk_id,
         membership_by_id[row.membership_id].match_fingerprint)
        for row in evidence_decisions if row.membership_id in membership_by_id
    ]
    document_query = select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.scope == "document",
            EntityCorrectionDecision.document_id == document_id,
            EntityCorrectionDecision.match_fingerprint.in_(fingerprints),
        ).order_by(EntityCorrectionDecision.id).limit(201)
    if for_update:
        document_query = document_query.with_for_update()
    document_rows = list((await session.scalars(
        document_query.execution_options(populate_existing=True)
    )).all())
    by_document_fingerprint: dict[str, list[EntityCorrectionDecision]] = {}
    for row in document_rows:
        if row.match_fingerprint is not None:
            by_document_fingerprint.setdefault(row.match_fingerprint, []).append(row)
    current_candidates_by_selector: dict[tuple[str, UUID], list[str]] = {}
    current_candidates_by_fingerprint: dict[str, list[str]] = {}
    for request_key, (fingerprint, chunks) in candidates.items():
        current_candidates_by_fingerprint.setdefault(fingerprint, []).append(request_key)
        for chunk_id in chunks:
            current_candidates_by_selector.setdefault((fingerprint, chunk_id), []).append(request_key)
    by_candidate: dict[str, list[tuple[EntityCorrectionDecision, UUID, bool]]] = {}
    for row, chunk_id, membership_fingerprint in evidence_rows:
        for request_key, (fingerprint, chunks) in candidates.items():
            if membership_fingerprint == fingerprint and chunk_id in chunks:
                by_candidate.setdefault(request_key, []).append((
                    row, chunk_id, row.match_fingerprint == fingerprint,
                ))
    result: dict[str, tuple[UUID, str, UUID | None] | None] = {}
    for request_key, (fingerprint, chunks) in candidates.items():
        all_evidence = by_candidate.get(request_key, [])
        evidence_matches = [
            (row, chunk) for row, chunk, exact_fingerprint in all_evidence
            if exact_fingerprint
        ]
        document_matches = by_document_fingerprint.get(fingerprint, [])
        evidence_states = {(row.decision, row.entity_id) for row, _ in evidence_matches}
        document_states = {(row.decision, row.entity_id) for row in document_matches}
        partial = bool(all_evidence) and {chunk for _, chunk in evidence_matches} != chunks
        fingerprint_ambiguous = any(not exact_fingerprint for _, _, exact_fingerprint in all_evidence)
        membership_ambiguous = any(
            len(memberships_by_selector.get((fingerprint, chunk_id), [])) > 1
            for chunk_id in chunks
        )
        current_evidence_ambiguous = any(
            len(current_candidates_by_selector.get((fingerprint, chunk_id), [])) > 1
            for chunk_id in chunks
        )
        current_document_ambiguous = (
            bool(document_matches)
            and len(current_candidates_by_fingerprint.get(fingerprint, [])) > 1
        )
        conflicting = (
            len(memberships) > 200 or len(evidence_decisions) > 200
            or len(document_rows) > 200 or partial or fingerprint_ambiguous
            or membership_ambiguous or current_evidence_ambiguous
            or current_document_ambiguous or len(evidence_states) > 1
            or len(document_states) > 1
        )
        conflicting = conflicting or bool(evidence_states and document_states and evidence_states != document_states)
        all_matches = [row for row, _, _ in all_evidence] + document_matches
        if conflicting:
            latest = max(all_matches, key=lambda row: (row.created_at, row.id)) if all_matches else None
            result[request_key] = (latest.id if latest else UUID(int=0), "conflict", None)
        elif not all_matches:
            result[request_key] = None
        else:
            latest = max(all_matches, key=lambda row: (row.created_at, row.id))
            result[request_key] = (latest.id, latest.decision, latest.entity_id)
    return result


async def create_entity(session: AsyncSession, payload: EntityCreate, *, actor_id: int) -> EntityRead:
    """Commit owner fields, aliases, audit and temporal desired-state change atomically."""
    entity = Entity(
        type=payload.type,
        name=payload.name,
        canonical_name=canonicalize_name(payload.name),
        description=payload.description,
        name_origin="owner",
        description_origin="owner" if payload.description is not None else None,
        metadata_json=payload.metadata,
    )
    session.add(entity)
    try:
        await session.flush()
        aliases = [
            EntityAlias(
                entity_id=entity.id,
                alias=alias,
                normalized_alias=canonicalize_name(alias),
                confirmed=True,
                origin="owner",
            )
            for alias in payload.aliases
            if canonicalize_name(alias) != entity.canonical_name
        ]
        session.add_all(aliases)
        await session.flush()
        await session.refresh(entity)
        result = _entity_read(entity, aliases)
        await record_owner_action(
            session, actor_id=actor_id, operation="entity_create", reason=payload.reason,
            affected_ids=[entity.id], revisions={str(entity.id): 1},
        )
        await _schedule_entity_change(session, entity, ["name", "description", "metadata", "aliases"], "owner")
        await commit_with_replay(session, [make_graph_change(entity_id=entity.id)])
    except IntegrityError:
        await session.rollback()
        raise
    return result


async def update_entity(
    session: AsyncSession, entity_id: UUID, payload: EntityPatch, *, actor_id: int
) -> EntityRead | None:
    """Apply a canonical owner-field update with revision and redirect fences.

    The owner-write route enforces authorization; ``actor_id`` is audit
    provenance. Returns None if the row disappears, rejects merged/deleted IDs
    with typed conflicts and stale revisions with ValueError, marks edited fields
    owner-authored, removes their derived field support, then commits audit and
    graph changes plus exact-support temporal desired state in one transaction.
    """
    try:
        canonical_id = await resolve_canonical_entity_id(session, entity_id)
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(EntityRedirect.old_entity_id == entity_id)) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(Entity.id == entity_id).with_for_update())
    if entity is None:
        return None
    previous_revision = entity.revision
    if entity.revision != payload.expected_revision:
        raise ValueError("Entity revision is stale")
    if not payload.model_fields_set - {"expected_revision", "reason"}:
        raise ValueError("At least one entity field is required")
    if "name" in payload.model_fields_set and payload.name is not None:
        entity.name = payload.name
        entity.canonical_name = canonicalize_name(payload.name)
        entity.name_origin = "owner"
        await session.execute(delete(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.field_name == "name",
        ))
    if "description" in payload.model_fields_set:
        entity.description = payload.description
        entity.description_origin = "owner"
        await session.execute(delete(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.field_name == "description",
        ))
    if "metadata" in payload.model_fields_set and payload.metadata is not None:
        entity.metadata_json = payload.metadata
    entity.revision += 1
    await session.flush()
    await session.refresh(entity)
    aliases = await _aliases(session, [entity.id])
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, actor_id=actor_id, operation="entity_update", reason=payload.reason,
        affected_ids=[entity.id], revisions={str(entity.id): previous_revision},
    )
    await _schedule_entity_change(session, entity, sorted(payload.model_fields_set - {"expected_revision", "reason"}), "owner")
    await commit_with_replay(session, [make_graph_change(entity_id=entity.id)])
    return result


async def add_alias(
    session: AsyncSession, entity_id: UUID, payload: AliasCreate, *, actor_id: int
) -> EntityRead | None:
    """Add an owner-authored alias to a canonical entity and commit its audit.

    The owner-write route authorizes the operation. Redirected or terminal IDs
    raise typed conflicts; a missing canonical row returns None. A successful
    insert records the actor/reason and atomically schedules graph desired state.
    """
    try:
        canonical_id = await resolve_canonical_entity_id(session, entity_id)
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(EntityRedirect.old_entity_id == entity_id)) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(Entity.id == entity_id).with_for_update())
    if entity is None:
        return None
    normalized = canonicalize_name(payload.alias)
    if normalized == entity.canonical_name:
        raise ValueError("Alias duplicates the canonical entity name")
    alias = EntityAlias(
        entity_id=entity.id,
        alias=payload.alias,
        normalized_alias=normalized,
        confirmed=payload.confirmed,
        origin="owner",
    )
    session.add(alias)
    await session.flush()
    aliases = await _aliases(session, [entity.id])
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, actor_id=actor_id, operation="alias_create", reason=payload.reason,
        affected_ids=[entity.id, alias.id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(session, entity, ["aliases"], "owner")
    await commit_with_replay(session, [make_graph_change(entity_id=entity.id)])
    return result


async def delete_alias(
    session: AsyncSession, entity_id: UUID, alias_id: UUID, *, actor_id: int, reason: str = "owner_alias_delete"
) -> bool:
    """Delete one alias from a canonical entity and commit its owner audit.

    The owner-write route authorizes the operation. Redirected or terminal IDs
    raise typed conflicts; a missing entity or alias returns False. Success
    records the actor/reason and atomically schedules graph desired state.
    """
    try:
        canonical_id = await resolve_canonical_entity_id(session, entity_id)
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(EntityRedirect.old_entity_id == entity_id)) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(Entity.id == entity_id).with_for_update())
    if entity is None:
        return False
    alias = await session.scalar(
        select(EntityAlias).where(EntityAlias.id == alias_id, EntityAlias.entity_id == entity_id).with_for_update()
    )
    if alias is None:
        return False
    await session.delete(alias)
    await record_owner_action(
        session, actor_id=actor_id, operation="alias_delete", reason=reason,
        affected_ids=[entity.id, alias_id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(session, entity, ["aliases"], "owner")
    await commit_with_replay(session, [make_graph_change(entity_id=entity.id)])
    return True


async def delete_entity(
    session: AsyncSession, entity_id: UUID, *, actor_id: int, reason: str = "owner_entity_delete"
) -> bool:
    """Delegate canonical deletion and its support cleanup to the correction owner."""
    from modules.knowledge.entities.corrections import delete_canonical_entity

    return await delete_canonical_entity(session, entity_id, actor_id=actor_id, reason=reason)


async def support_cleanup_ids(
    session: AsyncSession, *, document_id: UUID | None = None, source_id: UUID | None = None
) -> tuple[list[UUID], list[UUID]]:
    """Return bounded membership and entity lock IDs for one document/source cleanup."""
    if (document_id is None) == (source_id is None):
        raise ValueError("Specify one document or source")
    statement = select(EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id)
    statement = statement.where(
        EntityEvidenceMembership.document_id == document_id
        if document_id is not None else EntityEvidenceMembership.source_id == source_id
    ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(10_001)
    rows = list((await session.execute(statement)).all())
    if len(rows) > 10_000:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    entity_ids = {entity_id for _, entity_id in rows}
    if source_id is not None:
        entity_ids.update((await session.scalars(
            select(EntityAlias.entity_id).where(EntityAlias.source_id == source_id).distinct()
        )).all())
    return [membership_id for membership_id, _ in rows], sorted(entity_ids, key=str)


async def lock_entity_ids(session: AsyncSession, entity_ids: list[UUID]) -> None:
    """Lock a bounded sorted set of entity rows for support cleanup."""
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 10_000:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    if ids:
        await session.scalars(
            select(Entity.id).where(Entity.id.in_(ids)).order_by(Entity.id).with_for_update()
        )


async def remove_document_support(session: AsyncSession, document_id: UUID) -> int:
    """Remove evidence memberships and unsupported derived fields for one document."""
    return await _remove_entity_support(session, document_id=document_id)


async def remove_source_support(session: AsyncSession, source_id: UUID) -> int:
    """Remove evidence memberships and unsupported derived fields for one source."""
    return await _remove_entity_support(session, source_id=source_id)


async def _remove_entity_support(
    session: AsyncSession, *, document_id: UUID | None = None, source_id: UUID | None = None
) -> int:
    """Delete scoped evidence and aliases, preserving owner aliases and supported values."""
    if (document_id is None) == (source_id is None):
        raise ValueError("Specify one document or source")
    membership_query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.document_id == document_id
        if document_id is not None else EntityEvidenceMembership.source_id == source_id
    ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(10_001)
    memberships = list((await session.scalars(membership_query)).all())
    if len(memberships) > 10_000:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    membership_ids = [item.id for item in memberships]
    entity_ids = {item.entity_id for item in memberships}
    alias_ids: set[UUID] = set()
    if membership_ids:
        alias_supports = list((await session.execute(
            select(EntityAliasEvidence.id, EntityAliasEvidence.alias_id)
            .where(EntityAliasEvidence.membership_id.in_(membership_ids))
            .order_by(EntityAliasEvidence.id)
            .limit(10_001)
        )).all())
        if len(alias_supports) > 10_000:
            raise ValueError("Entity alias support cleanup exceeds its atomic limit")
        alias_ids.update(alias_id for _, alias_id in alias_supports)
        await session.execute(
            delete(EntityAliasEvidence).where(
                EntityAliasEvidence.id.in_([support_id for support_id, _ in alias_supports])
            )
        )
        await session.execute(
            delete(EntityEvidenceMembership).where(EntityEvidenceMembership.id.in_(membership_ids))
        )
    if source_id is not None:
        sourced = list((await session.scalars(
            select(EntityAlias).where(EntityAlias.source_id == source_id).order_by(EntityAlias.id).limit(10_001)
        )).all())
        if len(sourced) > 10_000:
            raise ValueError("Entity alias cleanup exceeds its atomic limit")
        alias_ids.update(item.id for item in sourced)
        entity_ids.update(item.entity_id for item in sourced)
    for alias_id in sorted(alias_ids, key=str):
        alias = await session.get(EntityAlias, alias_id)
        if alias is None:
            continue
        if source_id is not None and alias.source_id == source_id:
            alias.source_id = None
        remaining_confidence = await session.scalar(
            select(func.max(EntityAliasEvidence.confidence))
            .join(
                EntityEvidenceMembership,
                EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
            )
            .where(
                EntityAliasEvidence.alias_id == alias.id,
                EntityEvidenceMembership.entity_id == alias.entity_id,
            )
        )
        if alias.origin == "owner":
            continue
        if remaining_confidence is not None:
            alias.confidence = remaining_confidence
            continue
        # Origin-less legacy aliases carry no proof of owner authorship. Forget
        # them with their final exact support instead of upgrading their status.
        await session.delete(alias)
    await _clear_unsupported_derived_fields(session, entity_ids)
    return len(membership_ids)


async def _clear_unsupported_derived_fields(session: AsyncSession, entity_ids: set[UUID]) -> None:
    """Clear non-owner entity fields whose exact current evidence support was removed."""
    for entity_id in entity_ids:
        entity = await session.get(Entity, entity_id)
        if entity is not None:
            changed = False
            # Unknown legacy provenance is not proof that a non-owner value is safe to retain.
            if entity.name_origin != "owner" and entity.name is not None:
                name_hash = sha256(entity.name.encode("utf-8")).hexdigest()
                name_supported = entity.name_origin == "derived" and await session.scalar(
                    select(EntityFieldEvidence.id)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                    )
                    .where(
                        EntityFieldEvidence.entity_id == entity_id,
                        EntityFieldEvidence.field_name == "name",
                        EntityFieldEvidence.value_hash == name_hash,
                        EntityEvidenceMembership.entity_id == entity_id,
                    ).limit(1)
                ) is not None
                if not name_supported:
                    entity.name = None
                    entity.canonical_name = None
                    entity.name_origin = None
                    changed = True
            elif entity.name is None and entity.name_origin != "owner":
                changed = entity.canonical_name is not None or changed
                entity.canonical_name = None
                entity.name_origin = None
            if entity.description_origin != "owner" and entity.description is not None:
                description_hash = sha256(entity.description.encode("utf-8")).hexdigest()
                description_supported = entity.description_origin == "derived" and await session.scalar(
                    select(EntityFieldEvidence.id)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
                    )
                    .where(
                        EntityFieldEvidence.entity_id == entity_id,
                        EntityFieldEvidence.field_name == "description",
                        EntityFieldEvidence.value_hash == description_hash,
                        EntityEvidenceMembership.entity_id == entity_id,
                    ).limit(1)
                ) is not None
                if not description_supported:
                    entity.description = None
                    entity.description_origin = None
                    changed = True
            elif entity.description is None and entity.description_origin != "owner":
                entity.description_origin = None
            if changed:
                entity.revision += 1


async def list_changed_entities_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
) -> list[tuple[datetime, UUID, str, dict[str, str]]]:
    """Read-only cursor page of entities by ``(updated_at, id)`` for the automations sweep.

    The key combines id and ``updated_at`` so every change is a distinct trigger event. Payload
    carries id, type and a created/updated marker only.
    """
    stmt = select(Entity)
    if position is not None:
        stmt = stmt.where(tuple_(Entity.updated_at, Entity.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(Entity.updated_at, Entity.id).limit(limit))).all()
    return [(r.updated_at, r.id, f"{r.id}:{r.updated_at.isoformat()}",
             {"entity_id": str(r.id), "entity_type": r.type,
              "change": "created" if r.revision == 1 else "updated"}) for r in rows]
