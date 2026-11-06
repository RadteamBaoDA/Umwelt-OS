"""Relationship export pagination, cursors and fence validation.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

import binascii
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Select,
    exists,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.relationships._common import (
    _admit,
    _evidence_scope,
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
)
from modules.knowledge.relationships.schemas import (
    RelationshipExportEvidence,
    RelationshipExportFence,
    RelationshipExportFenceValidation,
    RelationshipExportPage,
    RelationshipExportRead,
    RelationshipSourceExportFence,
)
from modules.sources import public as sources
from modules.sources.public import SourceExportFence

RELATIONSHIP_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _encode_relationship_export_cursor(
    owner_id: int,
    workspace_id: UUID,
    snapshot_at: datetime,
    position_at: datetime,
    position_id: UUID,
) -> str:
    """Encode a canonical relationship cursor bound to owner and fixed snapshot."""
    raw = json.dumps(
        {
            "v": 1,
            "owner": owner_id,
            "workspace": str(workspace_id),
            "kind": "relationships",
            "snapshot": snapshot_at.astimezone(UTC).isoformat(),
            "at": position_at.astimezone(UTC).isoformat(),
            "id": str(position_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return b64url_encode(raw)


def _decode_relationship_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID
) -> tuple[datetime, datetime, UUID]:
    """Reject malformed, overlong, future, or cross-owner relationship cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "v",
            "owner",
            "workspace",
            "kind",
            "snapshot",
            "at",
            "id",
        }:
            raise ValueError
        if (
            value["v"] != 1
            or value["owner"] != owner_id
            or value["workspace"] != str(workspace_id)
            or value["kind"] != "relationships"
        ):
            raise ValueError
        snapshot_at, position_at = (
            datetime.fromisoformat(value["snapshot"]),
            datetime.fromisoformat(value["at"]),
        )
        if any(
            item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, position_at)
        ):
            raise ValueError
        snapshot_at, position_at = snapshot_at.astimezone(UTC), position_at.astimezone(UTC)
        if snapshot_at > datetime.now(UTC):
            raise ValueError
        position_id = UUID(value["id"])
        if (
            _encode_relationship_export_cursor(
                owner_id, workspace_id, snapshot_at, position_at, position_id
            )
            != cursor
        ):
            raise ValueError
        return snapshot_at, position_at, position_id
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Invalid relationship export cursor") from exc


def _relationship_export_bytes(items: list[RelationshipExportRead]) -> int:
    """Measure exact compact JSON bytes for the immutable relationship page."""
    return len(
        json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _relationship_export_statement(snapshot_at: datetime, *, scope: Scope) -> Select[Any]:
    """Select workspace relationships plus derived rows with retained eligible citations."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    retained_support = exists(
        select(RelationshipEvidence.id).where(
            _evidence_scope(scope),
            RelationshipEvidence.relationship_id == Relationship.id,
            RelationshipEvidence.source_id.in_(eligible),
        )
    )
    return select(Relationship).where(
        Relationship.workspace_id == scope.workspace_id,
        Relationship.created_at <= snapshot_at,
        Relationship.updated_at <= snapshot_at,
        or_(Relationship.origin == "owner", retained_support),
    )


async def _relationship_export_count(
    session: AsyncSession, snapshot_at: datetime, *, scope: Scope
) -> int:
    """Count portable workspace relationships visible at the captured source-purge boundary."""
    statement = (
        _relationship_export_statement(snapshot_at, scope=scope)
        .with_only_columns(func.count())
        .order_by(None)
    )
    return int(await session.scalar(statement) or 0)


async def _relationship_source_generations(
    session: AsyncSession,
    source_ids: set[UUID],
    *,
    scope: Scope,
) -> dict[UUID, int]:
    """Read eligible source generations through the owner-public lifecycle projection."""
    if not source_ids:
        return {}
    projection = sources.ingestion_lifecycle_projection(scope=scope).subquery()
    rows: Any = (
        await session.execute(
            select(projection.c.id, projection.c.generation).where(
                projection.c.id.in_(source_ids),
                projection.c.id.in_(sources.export_eligible_source_ids(scope=scope)),
            )
        )
    ).all()
    result = {source_id: int(generation) for source_id, generation in rows}
    if result.keys() != source_ids:
        raise ValueError("Relationship citation source is purging or no longer retained")
    return result


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
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
        snapshot_at, position_at, position_id = _decode_relationship_export_cursor(
            cursor, owner_id, scope.workspace_id
        )
        position = (position_at, position_id)
    snapshot_count = await _relationship_export_count(session, snapshot_at, scope=scope)
    statement = _relationship_export_statement(snapshot_at, scope=scope)
    if position is not None:
        statement = statement.where(tuple_(Relationship.created_at, Relationship.id) > position)
    rows = list(
        (
            await session.scalars(
                statement.order_by(Relationship.created_at, Relationship.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more = len(rows) > limit
    items: list[RelationshipExportRead] = []
    fences: list[RelationshipExportFence] = []
    eligible = sources.export_eligible_source_ids(scope=scope)
    for row in rows[:limit]:
        supports = list(
            (
                await session.scalars(
                    select(RelationshipEvidence)
                    .where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id == row.id,
                        RelationshipEvidence.source_id.in_(eligible),
                    )
                    .order_by(RelationshipEvidence.id)
                    .limit(101)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(supports) > 100:
            raise ValueError("A relationship export record exceeds the citation bound")
        generations = await _relationship_source_generations(
            session,
            {support.source_id for support in supports if support.source_id is not None},
            scope=scope,
        )
        refs = [
            RelationshipExportEvidence(
                id=support.id,
                source_id=support.source_id,
                source_generation=generations.get(support.source_id)
                if support.source_id is not None
                else None,
                document_id=support.document_id,
                document_version_id=support.document_version_id,
                chunk_id=support.chunk_id,
                source_membership_id=support.source_membership_id,
                target_membership_id=support.target_membership_id,
                observed_at=support.observed_at,
                confidence=support.confidence,
            )
            for support in supports
        ]
        item = RelationshipExportRead(
            id=row.id,
            source_entity_id=row.source_entity_id,
            target_entity_id=row.target_entity_id,
            type=row.type,
            origin=row.origin,
            confidence=row.confidence,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            created_at=row.created_at,
            updated_at=row.updated_at,
            evidence=refs,
        )
        if _relationship_export_bytes(items + [item]) > RELATIONSHIP_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("A relationship export record exceeds the page byte budget")
            has_more = True
            break
        items.append(item)
        digest = sha256(
            json.dumps(
                [ref.model_dump(mode="json") for ref in refs],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        fences.append(
            RelationshipExportFence(
                id=row.id,
                created_at=row.created_at,
                updated_at=row.updated_at,
                evidence_ids=[ref.id for ref in refs],
                source_fences=[
                    RelationshipSourceExportFence(source_id=source_id, generation=generation)
                    for source_id, generation in sorted(
                        generations.items(), key=lambda pair: str(pair[0])
                    )
                ],
                evidence_digest=digest,
            )
        )
    next_cursor = (
        _encode_relationship_export_cursor(
            owner_id, scope.workspace_id, snapshot_at, items[-1].created_at, items[-1].id
        )
        if has_more and items
        else None
    )
    return RelationshipExportPage(
        owner_id=owner_id,
        record_kind="relationships",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        items=items,
        fences=fences,
        payload_bytes=_relationship_export_bytes(items),
        max_payload_bytes=RELATIONSHIP_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[RelationshipExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RelationshipExportFenceValidation:
    """Recheck owner count, canonical row state, source generations, and exact citations."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "relationships" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Relationship export revalidation input is invalid")
    if owner_id != _actor(scope):
        return RelationshipExportFenceValidation(
            valid=False, reason="owner_unavailable", observed_snapshot_count=0
        )
    observed = await _relationship_export_count(session, snapshot_at, scope=scope)
    if observed != expected_snapshot_count:
        return RelationshipExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    eligible = sources.export_eligible_source_ids(scope=scope)
    for fence in fences:
        row = await session.scalar(
            select(Relationship)
            .where(
                Relationship.workspace_id == scope.workspace_id,
                Relationship.id == fence.id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None or (row.created_at, row.updated_at) != (fence.created_at, fence.updated_at):
            return RelationshipExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        source_fences = [
            SourceExportFence(
                source_id=item.source_id,
                workspace_id=scope.workspace_id,
                generation=item.generation,
            )
            for item in fence.source_fences
        ]
        if set(
            await sources.filter_export_eligible_sources(
                session, source_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        ) != {item.source_id for item in source_fences}:
            return RelationshipExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        supports = list(
            (
                await session.scalars(
                    select(RelationshipEvidence)
                    .where(
                        _evidence_scope(scope),
                        RelationshipEvidence.relationship_id == fence.id,
                        RelationshipEvidence.source_id.in_(eligible),
                    )
                    .order_by(RelationshipEvidence.id)
                    .limit(101)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(supports) > 100:
            return RelationshipExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        generations = await _relationship_source_generations(
            session,
            {support.source_id for support in supports if support.source_id is not None},
            scope=scope,
        )
        refs = [
            RelationshipExportEvidence(
                id=support.id,
                source_id=support.source_id,
                source_generation=generations.get(support.source_id)
                if support.source_id is not None
                else None,
                document_id=support.document_id,
                document_version_id=support.document_version_id,
                chunk_id=support.chunk_id,
                source_membership_id=support.source_membership_id,
                target_membership_id=support.target_membership_id,
                observed_at=support.observed_at,
                confidence=support.confidence,
            )
            for support in supports
        ]
        digest = sha256(
            json.dumps(
                [ref.model_dump(mode="json") for ref in refs],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if [ref.id for ref in refs] != fence.evidence_ids or digest != fence.evidence_digest:
            return RelationshipExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return RelationshipExportFenceValidation(
        valid=True, reason="valid", observed_snapshot_count=observed
    )
