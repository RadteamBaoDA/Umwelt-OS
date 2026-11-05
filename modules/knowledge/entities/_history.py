"""Temporal node seeds and entity change-history reads.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

import binascii
import json
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities._reads import (
    get_entity_refs,
    get_membership_refs,
    list_entity_evidence,
    resolve_canonical_entity_id,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityOwnerAction,
)
from modules.knowledge.entities.schemas import (
    EntityHistoryItem,
    EntityHistoryPage,
    EntityTemporalNodeSeed,
)
from modules.sources import public as sources


async def get_temporal_node_seeds(
    session: AsyncSession, membership_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[EntityTemporalNodeSeed, ...]:
    """Prove current nonblank fields against exact selected source-local evidence.

    Requires caller-held source/document fences and current egress policy. Up to
    100 unique memberships must belong to one active source generation. Missing,
    stale, redirected or unsupported fields fail closed; no writes or commits.
    Owner authorship alone is not source-local evidence for model seed text.
    """
    """Return bounded current temporal node seeds backed by live exact evidence.

    The scope is admitted before reading memberships, and every membership,
    entity, source, document fence, and evidence lookup stays inside that
    workspace. This is read-only and does not commit the caller's transaction.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not membership_ids or len(membership_ids) > 100 or len(set(membership_ids)) != len(membership_ids):
        raise ValueError("Node seed memberships must contain 1 to 100 unique IDs")
    rows = list((await session.scalars(select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.id.in_(membership_ids),
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
    ).order_by(EntityEvidenceMembership.id))).all())
    if len(rows) != len(membership_ids) or len({row.source_id for row in rows}) != 1:
        raise LookupError("Node seed memberships are missing or cross-source")
    source = await sources.get_connector_source(
        session, rows[0].source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.status != "active":
        raise LookupError("Node seed source is unavailable")
    from modules.knowledge.documents import public as documents
    groups: dict[tuple[UUID, UUID], list[UUID]] = {}
    for row in rows:
        groups.setdefault((row.document_id, row.document_version_id), []).append(row.chunk_id)
    for (document_id, version_id), chunks in groups.items():
        fences = await documents.review_version_fences(
            session, [version_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        fence = fences.get(version_id)
        refs = await documents.read_evidence_refs(
            session, [(version_id, chunk) for chunk in dict.fromkeys(chunks)],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if (fence is None or fence.document_id != document_id or fence.source_id != source.id
                or fence.current_source_generation != source.generation
                or len(refs) != len(set(chunks))
                or any(ref.document_id != document_id or ref.source_id != source.id for ref in refs)):
            raise LookupError("Node seed retained evidence is not current and permitted")
    result = []
    for entity_id in sorted({row.entity_id for row in rows}):
        ref = (await get_entity_refs(
            session, [entity_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ))[0]
        if ref.canonical_id != entity_id:
            raise LookupError("Node seed identity was redirected")
        entity = await session.scalar(select(Entity).where(
            Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
        ))
        if entity is None or not entity.name or not entity.name.strip() or entity.name_origin is None:
            raise LookupError("Node seed name is unavailable")
        selected = [row for row in rows if row.entity_id == entity_id]
        proofs = list((await session.scalars(select(EntityFieldEvidence).join(
            EntityEvidenceMembership, EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
        ).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.membership_id.in_([row.id for row in selected]),
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
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
            memberships=await get_membership_refs(
                session, [row.id for row in selected], scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            ),
            name_support_membership_ids=name_support, summary_support_membership_ids=summary_support,
            name_hash=name_hash, summary_hash=summary_hash if summary_support else None,
        ))
    if sum(len((seed.name + (seed.summary or "")).encode("utf-8")) for seed in result) > 64_000:
        raise ValueError("Node seed text exceeds its 64000-byte aggregate bound")
    return tuple(result)


def _decode_identity_cursor(cursor: str, entity_id: UUID, message: str) -> tuple[str, tuple[datetime, UUID]]:
    """Decode an entity-bound history cursor ``[entity_id, position]`` (<= 1,024 chars, strict base64).

    Returns the inner position string and its decoded ``(timestamp, id)``. Any malformation, foreign entity
    identity or bad position raises ``ValueError(message)``.
    """
    try:
        if len(cursor) > 1024:
            raise ValueError
        identity, position = json.loads(b64url_decode(cursor))
        if identity != str(entity_id):
            raise ValueError
        return position, decode_cursor(position)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError(message) from exc


async def list_entity_history(
    session: AsyncSession, entity_id: UUID, limit: int = 50, cursor: str | None = None,
    *, membership_cursor: str | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> EntityHistoryPage | None:
    """Page identifier-only owner audit for a currently accessible canonical entity.

    No reason/raw historical values are exposed. Audit timestamps describe edits,
    not occurrence; retained evidence remains available via list_entity_evidence.
    Bound cursor to requested identity and never manufacture past field values.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Entity history page limit must be between 1 and 100")
    try:
        canonical = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        return None
    statement = select(EntityOwnerAction).where(
        EntityOwnerAction.workspace_id == scope.workspace_id,
        or_(
        EntityOwnerAction.affected_ids.contains([str(entity_id)]),
        EntityOwnerAction.affected_ids.contains([str(canonical)]),
    ))
    if cursor:
        _, (timestamp, identifier) = _decode_identity_cursor(cursor, entity_id, "Invalid entity history cursor")
        statement = statement.where(tuple_(EntityOwnerAction.created_at, EntityOwnerAction.id) < (timestamp, identifier))
    rows = list((await session.scalars(statement.order_by(
        EntityOwnerAction.created_at.desc(), EntityOwnerAction.id.desc(),
    ).limit(limit + 1))).all())
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        position = encode_cursor(rows[-1].created_at, rows[-1].id)
        next_cursor = b64url_encode(json.dumps([str(entity_id), position]).encode())
    membership_position = None
    if membership_cursor:
        membership_position, _ = _decode_identity_cursor(
            membership_cursor, entity_id, "Invalid entity membership history cursor")
    membership_page = await list_entity_evidence(
        session, canonical, limit, membership_position, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if membership_page:
        permitted = set()
        for source_id in sorted({item.source_id for item in membership_page.items}):
            source = await sources.get_connector_source(
                session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            if source is not None and source.status == "active":
                permitted.add(source_id)
        membership_page.items = [item for item in membership_page.items if item.source_id in permitted]
    membership_next = None
    if membership_page and membership_page.next_cursor:
        membership_next = b64url_encode(json.dumps([
            str(entity_id), membership_page.next_cursor,
        ]).encode())
    return EntityHistoryPage(items=[EntityHistoryItem(
        id=row.id, recorded_at=row.created_at, operation=row.operation,
        affected_ids=row.affected_ids, revisions=row.revisions,
    ) for row in rows], next_cursor=next_cursor,
        memberships=membership_page.items if membership_page else [],
        membership_next_cursor=membership_next)
