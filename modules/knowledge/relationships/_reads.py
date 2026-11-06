"""Relationship, evidence and neighbor list reads.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

import binascii
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import (
    desc,
    func,
    or_,
    select,
    tuple_,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities import public as entities
from modules.knowledge.relationships._common import (
    _admit,
    _evidence_read,
    _evidence_scope,
    _relationship_read,
)
from modules.knowledge.relationships._history import (
    _decode_bound_cursor,
    _list_relationships_as_of,
    get_relationship_snapshot,
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
)
from modules.knowledge.relationships.schemas import (
    EntityGraphRead,
    EvidenceRead,
    NeighborPage,
    NeighborRead,
    RelationshipPage,
)
from modules.sources import public as sources


async def list_relationships(
    session: AsyncSession,
    limit: int,
    cursor: str | None,
    entity_id: UUID | None = None,
    *,
    valid_at: datetime | None = None,
    include_unknown_validity: bool = True,
    knowledge_as_of: datetime | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
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
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    filters = [
        str(scope.workspace_id),
        str(entity_id) if entity_id else None,
        valid_at.isoformat() if valid_at else None,
        include_unknown_validity,
        knowledge_as_of.isoformat() if knowledge_as_of else None,
    ]
    fingerprint = sha256(json.dumps(filters, separators=(",", ":")).encode()).hexdigest()
    if knowledge_as_of is not None:
        return await _list_relationships_as_of(
            session,
            limit=limit,
            cursor=cursor,
            entity_id=entity_id,
            valid_at=valid_at,
            include_unknown_validity=include_unknown_validity,
            knowledge_as_of=knowledge_as_of,
            fingerprint=fingerprint,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    statement = select(Relationship).where(Relationship.workspace_id == scope.workspace_id)
    known = Relationship.valid_from.is_not(None) & Relationship.valid_to.is_not(None)
    if not include_unknown_validity:
        statement = statement.where(known)
    if valid_at is not None:
        in_interval = or_(
            Relationship.valid_from.is_(None), Relationship.valid_from <= valid_at
        ) & or_(
            Relationship.valid_to.is_(None),
            Relationship.valid_to > valid_at,
        )
        statement = statement.where(in_interval)
    if entity_id is not None:
        statement = statement.where(
            or_(
                Relationship.source_entity_id == entity_id,
                Relationship.target_entity_id == entity_id,
            )
        )
    if cursor:
        created_at, identifier = _decode_bound_cursor(
            cursor, fingerprint, "Invalid relationship filter cursor"
        )
        statement = statement.where(
            tuple_(Relationship.created_at, Relationship.id) < (created_at, identifier)
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(desc(Relationship.created_at), desc(Relationship.id)).limit(
                    limit + 1
                )
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = b64url_encode(
            json.dumps(
                [
                    fingerprint,
                    encode_cursor(rows[-1].created_at, rows[-1].id),
                ]
            ).encode()
        )
    items = []
    for row in rows:
        try:
            snapshot = await get_relationship_snapshot(
                session, row.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        except LookupError:
            continue
        if snapshot is None:
            continue
        item = snapshot.relationship
        items.append(item)
    return RelationshipPage(
        items=items,
        next_cursor=next_cursor,
        canonical_history_available=knowledge_as_of is None,
        knowledge_as_of=knowledge_as_of,
        observation_history_only=knowledge_as_of is not None,
    )


async def list_relationship_evidence(
    session: AsyncSession,
    relationship_id: UUID,
    limit: int,
    cursor: str | None,
    *,
    knowledge_as_of: datetime | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
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
    fingerprint = sha256(
        json.dumps(
            [str(relationship_id), knowledge_as_of.isoformat() if knowledge_as_of else None]
        ).encode()
    ).hexdigest()
    if (
        await session.scalar(
            select(Relationship.id).where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id == relationship_id,
            )
        )
        is None
    ):
        return None, None
    statement = select(RelationshipEvidence).where(
        _evidence_scope(scope),
        RelationshipEvidence.relationship_id == relationship_id,
    )
    if knowledge_as_of is not None:
        statement = statement.where(RelationshipEvidence.observed_at <= knowledge_as_of)
    if cursor:
        extracted_at, identifier = _decode_bound_cursor(
            cursor, fingerprint, "Invalid relationship observation cursor"
        )
        statement = statement.where(
            tuple_(RelationshipEvidence.extracted_at, RelationshipEvidence.id)
            > (extracted_at, identifier)
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(
                    RelationshipEvidence.extracted_at, RelationshipEvidence.id
                ).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = b64url_encode(
            json.dumps(
                [
                    fingerprint,
                    encode_cursor(rows[-1].extracted_at, rows[-1].id),
                ]
            ).encode()
        )
    evidence = await _evidence_read(
        session, rows, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    permitted = set()
    for source_id in sorted({ref.source_id for ref in evidence}):
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if source is not None and source.status == "active":
            permitted.add(source_id)
    return [
        ref
        for ref in evidence
        if ref.source_id in permitted
        and (knowledge_as_of is None or ref.observed_at <= knowledge_as_of)
    ], next_cursor


def _encode_neighbor_cursor(focus_id: UUID, created_at: datetime, relationship_id: UUID) -> str:
    """Bind a relationship cursor to its focus entity before encoding it."""
    raw = json.dumps(
        [str(focus_id), encode_cursor(created_at, relationship_id)], separators=(",", ":")
    ).encode()
    return b64url_encode(raw)


def _decode_neighbor_cursor(cursor: str, focus_id: UUID) -> tuple[object, UUID]:
    """Decode a canonical cursor and reject cursors created for another entity."""
    if len(cursor) > 512 or "=" in cursor:
        raise ValueError("Invalid neighbor cursor")
    try:
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError("Invalid neighbor cursor")
        focus, cursor_value = json.loads(raw)
        if focus != str(focus_id):
            raise ValueError("Neighbor cursor belongs to another entity")
        return decode_cursor(cursor_value)
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid neighbor cursor") from exc


def _neighbor_items(
    rows: list[Relationship], focus: UUID, by_id: dict[UUID, Any]
) -> list[NeighborRead]:
    """Project relationship rows around ``focus`` into ``NeighborRead`` items (pure, order preserving).

    ``by_id`` maps the neighbour requested id to its resolved entity reference; rows whose neighbour did not
    resolve are dropped.
    """
    return [
        NeighborRead(
            entity=EntityGraphRead(
                id=by_id[neighbor_id].canonical_id,
                type=by_id[neighbor_id].type,
                name=by_id[neighbor_id].name,
                revision=by_id[neighbor_id].revision,
            ),
            relationship=_relationship_read(row),
        )
        for row in rows
        if (
            neighbor_id := (
                row.target_entity_id if row.source_entity_id == focus else row.source_entity_id
            )
        )
        in by_id
    ]


async def get_neighbors(
    session: AsyncSession,
    entity_id: UUID,
    limit: int = 50,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> NeighborPage | None:
    """Return bounded adjacent entities with their connecting relationships."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 2 <= limit <= 100:
        raise ValueError("Neighbor page limit must be between 2 and 100 total nodes")
    try:
        focus = (
            await entities.get_entity_refs(
                session, [entity_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        )[0].canonical_id
    except LookupError:
        return None
    statement = select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id,
        or_(Relationship.source_entity_id == focus, Relationship.target_entity_id == focus),
    )
    if cursor:
        created_at, relationship_id = _decode_neighbor_cursor(cursor, focus)
        statement = statement.where(
            tuple_(Relationship.created_at, Relationship.id) < (created_at, relationship_id)
        )
    rows = list(
        (
            await session.scalars(
                statement.order_by(desc(Relationship.created_at), desc(Relationship.id)).limit(
                    limit + 1
                )
            )
        ).all()
    )
    truncated = len(rows) > limit - 1
    rows = rows[: limit - 1]
    neighbor_ids = list(
        dict.fromkeys(
            row.target_entity_id if row.source_entity_id == focus else row.source_entity_id
            for row in rows
        )
    )
    refs = (
        await entities.get_entity_refs(
            session,
            neighbor_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if neighbor_ids
        else []
    )
    by_id = {ref.requested_id: ref for ref in refs}
    items = _neighbor_items(rows, focus, by_id)
    next_cursor = (
        _encode_neighbor_cursor(focus, rows[-1].created_at, rows[-1].id)
        if truncated and rows
        else None
    )
    return NeighborPage(items=items, truncated=truncated, next_cursor=next_cursor)


async def _first_neighbor_rows(
    session: AsyncSession, wanted: list[UUID], limit: int, *, scope: Scope
) -> dict[UUID, list[Relationship]]:
    """Read the first ``limit`` relationships of every focus in one window-function query.

    Read-only; relationships come back ordered ``(created_at DESC, id DESC)`` per focus, and every
    focus in ``wanted`` has an entry (possibly empty). Scoped to the caller's workspace.

    """
    rows_by_focus: dict[UUID, list[Relationship]] = {focus: [] for focus in wanted}
    if wanted:
        # UNION (not UNION ALL) so a self-loop matched on both endpoints appears once per focus, like OR.
        matches = union(
            select(
                Relationship.source_entity_id.label("focus"),
                Relationship.id.label("rid"),
                Relationship.created_at.label("created"),
            ).where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.source_entity_id.in_(wanted),
            ),
            select(
                Relationship.target_entity_id.label("focus"),
                Relationship.id.label("rid"),
                Relationship.created_at.label("created"),
            ).where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.target_entity_id.in_(wanted),
            ),
        ).subquery()
        ranked = select(
            matches.c.focus,
            matches.c.rid,
            func.row_number()
            .over(
                partition_by=matches.c.focus,
                order_by=(matches.c.created.desc(), matches.c.rid.desc()),
            )
            .label("position"),
        ).subquery()
        found = (
            await session.execute(
                select(Relationship, ranked.c.focus)
                .join(ranked, ranked.c.rid == Relationship.id)
                .where(ranked.c.position <= limit)
                .order_by(ranked.c.focus, desc(Relationship.created_at), desc(Relationship.id))
            )
        ).all()
        for found_row in found:
            relationship: Relationship = found_row[0]
            focus_id: UUID = found_row[1]
            rows_by_focus[focus_id].append(relationship)
    return rows_by_focus


async def get_neighbors_bulk(
    session: AsyncSession,
    ids: list[UUID],
    limit: int = 50,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[NeighborPage | None]:
    """Bulk first-page ``get_neighbors``: pages aligned with ``ids`` (duplicates allowed, at most 100).

    Per entity the page equals ``get_neighbors(id, limit)`` without a cursor: the focus is the redirect-resolved
    canonical entity (``None`` page when missing), relationships are ordered ``(created_at DESC, id DESC)`` and
    cut at ``limit - 1`` with ``truncated``/``next_cursor`` set when more exist. One window-function query
    selects every focus's first ``limit`` relationships and neighbour entity references are resolved in
    batches of at most 100 unique ids, instead of ~4 statements per entity. Read-only; a dangling neighbour
    raises ``LookupError`` like the singular.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 2 <= limit <= 100:
        raise ValueError("Neighbor page limit must be between 2 and 100 total nodes")
    focuses = await entities.resolve_canonical_entity_ids(
        session,
        ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    wanted = list(dict.fromkeys(focus for focus in focuses if focus is not None))
    rows_by_focus = await _first_neighbor_rows(session, wanted, limit, scope=scope)
    neighbor_ids = list(
        dict.fromkeys(
            row.target_entity_id if row.source_entity_id == focus else row.source_entity_id
            for focus in wanted
            for row in rows_by_focus[focus][: limit - 1]
        )
    )
    by_id: dict[UUID, Any] = {}
    for start in range(0, len(neighbor_ids), 100):  # get_entity_refs accepts at most 100 unique ids
        for ref in await entities.get_entity_refs(
            session,
            neighbor_ids[start : start + 100],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            by_id[ref.requested_id] = ref
    pages: dict[UUID, NeighborPage] = {}
    for focus in wanted:
        focus_rows = rows_by_focus[focus]
        truncated = len(focus_rows) > limit - 1
        focus_rows = focus_rows[: limit - 1]
        next_cursor = (
            _encode_neighbor_cursor(focus, focus_rows[-1].created_at, focus_rows[-1].id)
            if truncated and focus_rows
            else None
        )
        pages[focus] = NeighborPage(
            items=_neighbor_items(focus_rows, focus, by_id),
            truncated=truncated,
            next_cursor=next_cursor,
        )
    return [pages[focus] if focus is not None else None for focus in focuses]
