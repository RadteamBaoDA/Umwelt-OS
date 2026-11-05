"""Cursor-paged entity export reads and export fence validation.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

import binascii
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityEvidenceMembership,
    EntityFieldEvidence,
)
from modules.knowledge.entities.schemas import (
    EntityExportAlias,
    EntityExportAliasEvidence,
    EntityExportEvidence,
    EntityExportFence,
    EntityExportFenceValidation,
    EntityExportPage,
    EntityExportRead,
    EntitySourceExportFence,
)
from modules.sources import public as sources
from modules.sources.public import SourceExportFence

ENTITY_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _encode_entity_export_cursor(
    owner_id: int,
    workspace_id: UUID,
    snapshot_at: datetime,
    position_at: datetime,
    position_id: UUID,
) -> str:
    """Encode an owner-, workspace- and snapshot-bound entity keyset position."""
    payload = json.dumps(
        {
            "v": 1,
            "owner": owner_id,
            "workspace": str(workspace_id),
            "kind": "entities",
            "snapshot": snapshot_at.astimezone(UTC).isoformat(),
            "at": position_at.astimezone(UTC).isoformat(),
            "id": str(position_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return b64url_encode(payload)


def _decode_entity_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID
) -> tuple[datetime, datetime, UUID]:
    """Decode a canonical bounded cursor, rejecting cross-owner and future snapshots."""
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
            or value["kind"] != "entities"
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
            _encode_entity_export_cursor(
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
        raise ValueError("Invalid entity export cursor") from exc


def _entity_export_payload_bytes(items: list[EntityExportRead]) -> int:
    """Measure the exact compact UTF-8 JSON array returned for an entity page."""
    return len(
        json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


async def _entity_export_count(
    session: AsyncSession,
    snapshot_at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Count cutoff-stable owner facts and entities backed by currently eligible source evidence."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    eligible_membership = exists(
        select(EntityEvidenceMembership.id).where(
            EntityEvidenceMembership.entity_id == Entity.id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        )
    )
    eligible_alias_support = exists(
        select(EntityAliasEvidence.id)
        .join(
            EntityEvidenceMembership,
            EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
        )
        .where(
            EntityAliasEvidence.alias_id == EntityAlias.id,
            EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        )
    )
    owner_alias = exists(
        select(EntityAlias.id).where(
            EntityAlias.entity_id == Entity.id,
            or_(EntityAlias.origin == "owner", eligible_alias_support),
        )
    )
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Entity)
            .where(
                Entity.workspace_id == scope.workspace_id,
                Entity.created_at <= snapshot_at,
                Entity.updated_at <= snapshot_at,
                or_(
                    Entity.name_origin == "owner",
                    Entity.description_origin == "owner",
                    eligible_membership,
                    owner_alias,
                ),
            )
        )
        or 0
    )


async def _entity_export_source_generations(
    session: AsyncSession,
    source_ids: set[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> dict[UUID, int]:
    """Read current eligible source generations through the public lifecycle projection."""
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
    generations = {source_id: int(generation) for source_id, generation in rows}
    if generations.keys() != source_ids:
        raise ValueError("Entity citation source is purging or no longer retained")
    return generations


async def _entity_export_field_is_supported(
    session: AsyncSession,
    entity: Entity,
    field_name: str,
    value: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Require one exact current-field support membership from an eligible source."""
    if value is None:
        return False
    value_hash = sha256(value.encode("utf-8")).hexdigest()
    return (
        await session.scalar(
            select(EntityFieldEvidence.id)
            .join(
                EntityEvidenceMembership,
                EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
            )
            .where(
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                EntityFieldEvidence.entity_id == entity.id,
                EntityFieldEvidence.field_name == field_name,
                EntityFieldEvidence.value_hash == value_hash,
                EntityEvidenceMembership.source_id.in_(
                    sources.export_eligible_source_ids(scope=scope)
                ),
            )
            .limit(1)
        )
        is not None
    )


def _entity_export_statement(
    scope: Scope,
    snapshot_at: datetime,
    position: tuple[datetime, UUID] | None,
    eligible: Any,
) -> Any:
    """Build the keyset select of exportable entities at the snapshot (owner-set fields or eligible support)."""
    eligible_membership = exists(
        select(EntityEvidenceMembership.id).where(
            EntityEvidenceMembership.entity_id == Entity.id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        )
    )
    eligible_alias_support = exists(
        select(EntityAliasEvidence.id)
        .join(
            EntityEvidenceMembership,
            EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
        )
        .where(
            EntityAliasEvidence.alias_id == EntityAlias.id,
            EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        )
    )
    owner_alias = exists(
        select(EntityAlias.id).where(
            EntityAlias.entity_id == Entity.id,
            or_(EntityAlias.origin == "owner", eligible_alias_support),
        )
    )
    statement = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        Entity.created_at <= snapshot_at,
        Entity.updated_at <= snapshot_at,
        or_(
            Entity.name_origin == "owner",
            Entity.description_origin == "owner",
            eligible_membership,
            owner_alias,
        ),
    )
    if position is not None:
        statement = statement.where(tuple_(Entity.created_at, Entity.id) > position)
    return statement


async def _entity_export_children(
    session: AsyncSession,
    row: Entity,
    eligible: Any,
    scope: Scope,
) -> tuple[list[EntityAlias], list[EntityEvidenceMembership], dict[UUID, list[Any]]]:
    """Read one entity's exportable aliases, citations and per-alias support rows within explicit bounds.

    Raises ``ValueError`` when aliases or citations exceed 100, any alias exceeds 100 support rows, or total
    alias support exceeds 100, so no fact is silently truncated.
    """
    aliases = list(
        (
            await session.scalars(
                select(EntityAlias)
                .where(
                    EntityAlias.entity_id == row.id,
                    or_(
                        EntityAlias.origin == "owner",
                        exists(
                            select(EntityAliasEvidence.id)
                            .join(
                                EntityEvidenceMembership,
                                EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
                            )
                            .where(
                                EntityAliasEvidence.alias_id == EntityAlias.id,
                                EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
                                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                                EntityEvidenceMembership.source_id.in_(eligible),
                            )
                        ),
                    ),
                )
                .order_by(EntityAlias.id)
                .limit(101)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    evidence = list(
        (
            await session.scalars(
                select(EntityEvidenceMembership)
                .where(
                    EntityEvidenceMembership.entity_id == row.id,
                    EntityEvidenceMembership.workspace_id == scope.workspace_id,
                    EntityEvidenceMembership.source_id.in_(eligible),
                )
                .order_by(EntityEvidenceMembership.id)
                .limit(101)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(aliases) > 100 or len(evidence) > 100:
        raise ValueError("An entity export record exceeds the alias or citation reference bound")
    alias_support_rows: dict[UUID, list[Any]] = {}
    for alias in aliases:
        support_rows = list(
            (
                await session.execute(
                    select(EntityAliasEvidence, EntityEvidenceMembership)
                    .join(
                        EntityEvidenceMembership,
                        EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
                    )
                    .where(
                        EntityAliasEvidence.alias_id == alias.id,
                        EntityEvidenceMembership.entity_id == alias.entity_id,
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityEvidenceMembership.source_id.in_(eligible),
                    )
                    .order_by(EntityAliasEvidence.id)
                    .limit(101)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(support_rows) > 100:
            raise ValueError("An entity alias exceeds the support reference bound")
        alias_support_rows[alias.id] = support_rows
    if sum(map(len, alias_support_rows.values())) > 100:
        raise ValueError("An entity export record exceeds the total alias support bound")
    return aliases, evidence, alias_support_rows


async def _entity_export_item(
    session: AsyncSession,
    row: Entity,
    eligible: Any,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[EntityExportRead, EntityExportFence]:
    """Build one entity's export record and its final-validation fence.

    Non-owner name/description are exported only with exact eligible support (otherwise blanked with their
    origin). Reads children, distinct source generations (<= 100) and field support in that order.
    """
    aliases, evidence, alias_support_rows = await _entity_export_children(
        session, row, eligible, scope
    )
    source_ids = {item.source_id for item in evidence}
    source_ids.update(
        membership.source_id
        for rows_for_alias in alias_support_rows.values()
        for _, membership in rows_for_alias
    )
    if len(source_ids) > 100:
        raise ValueError("An entity export record exceeds the distinct source generation bound")
    source_generations = await _entity_export_source_generations(
        session,
        source_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    alias_items = [
        EntityExportAlias(
            id=alias.id,
            alias=alias.alias,
            confirmed=alias.confirmed,
            origin=alias.origin,
            confidence=alias.confidence,
            created_at=alias.created_at,
            supports=[
                EntityExportAliasEvidence(
                    membership_id=membership.id,
                    source_id=membership.source_id,
                    source_generation=source_generations[membership.source_id],
                    confidence=support.confidence,
                )
                for support, membership in alias_support_rows[alias.id]
            ],
        )
        for alias in aliases
    ]
    evidence_items = [
        EntityExportEvidence(
            id=item.id,
            source_id=item.source_id,
            source_generation=source_generations[item.source_id],
            document_id=item.document_id,
            document_version_id=item.document_version_id,
            chunk_id=item.chunk_id,
            observed_at=item.observed_at,
            extracted_at=item.extracted_at,
            confidence=item.confidence,
        )
        for item in evidence
    ]
    name = row.name
    canonical_name = row.canonical_name
    name_origin = row.name_origin
    if row.name_origin != "owner" and not await _entity_export_field_is_supported(
        session,
        row,
        "name",
        row.name,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        name = canonical_name = None
        name_origin = None
    description = row.description
    description_origin = row.description_origin
    if row.description_origin != "owner" and not await _entity_export_field_is_supported(
        session,
        row,
        "description",
        row.description,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        description = None
        description_origin = None
    item = EntityExportRead(
        id=row.id,
        type=row.type,
        name=name,
        canonical_name=canonical_name,
        description=description,
        revision=row.revision,
        name_origin=name_origin,
        description_origin=description_origin,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        aliases=alias_items,
        evidence=evidence_items,
    )
    fence = EntityExportFence(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        revision=row.revision,
        alias_ids=[alias.id for alias in alias_items],
        evidence_ids=[ev.id for ev in evidence_items],
        source_fences=[
            EntitySourceExportFence(source_id=source_id, generation=generation)
            for source_id, generation in sorted(
                source_generations.items(), key=lambda pair: str(pair[0])
            )
        ],
        alias_digest=sha256(
            json.dumps(
                [alias.model_dump(mode="json") for alias in alias_items],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        evidence_digest=sha256(
            json.dumps(
                [ev.model_dump(mode="json") for ev in evidence_items],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    return item, fence


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityExportPage:
    """Return bounded canonical entity facts with aliases, citation IDs, and final-validation fences.

    Owner fields and owner-origin aliases survive independently; non-owner name and description
    values require exact current support from an export-eligible source. Derived and origin-less
    aliases likewise require exact eligible alias-support memberships. Creator source IDs, arbitrary
    metadata, extraction payloads and audit details are excluded. Combined citations are capped at 100;
    an overfull record fails explicitly so no canonical fact is silently truncated.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or record_kind != "entities" or not 1 <= limit <= 100:
        raise ValueError("Entity export kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_entity_export_cursor(
            cursor, owner_id, scope.workspace_id
        )
        position = (position_at, position_id)
    snapshot_count = await _entity_export_count(
        session,
        snapshot_at,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    eligible = sources.export_eligible_source_ids(scope=scope)
    statement = _entity_export_statement(scope, snapshot_at, position, eligible)
    rows = list(
        (
            await session.scalars(
                statement.order_by(Entity.created_at, Entity.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    has_more = len(rows) > limit
    items: list[EntityExportRead] = []
    fences: list[EntityExportFence] = []
    for row in rows[:limit]:
        item, fence = await _entity_export_item(
            session,
            row,
            eligible,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        proposed = _entity_export_payload_bytes(items + [item])
        if proposed > ENTITY_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An entity export record exceeds the page byte budget")
            has_more = True
            break
        items.append(item)
        fences.append(fence)
    next_cursor = (
        _encode_entity_export_cursor(
            owner_id, scope.workspace_id, snapshot_at, items[-1].created_at, items[-1].id
        )
        if has_more and items
        else None
    )
    return EntityExportPage(
        owner_id=owner_id,
        record_kind="entities",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        items=items,
        fences=fences,
        payload_bytes=_entity_export_payload_bytes(items),
        max_payload_bytes=ENTITY_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[EntityExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityExportFenceValidation:
    """Recheck the bounded entity set, revisions, aliases, and citation IDs before publication."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        owner_id != _actor(scope)
        or record_kind != "entities"
        or len(fences) > 100
        or expected_snapshot_count < 0
    ):
        raise ValueError("Entity export revalidation input is invalid")
    observed = await _entity_export_count(
        session,
        snapshot_at,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if observed != expected_snapshot_count:
        return EntityExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        row = await session.scalar(
            select(Entity)
            .where(
                Entity.id == fence.id,
                Entity.workspace_id == scope.workspace_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None or (row.created_at, row.updated_at, row.revision) != (
            fence.created_at,
            fence.updated_at,
            fence.revision,
        ):
            return EntityExportFenceValidation(
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
        eligible_source_ids = set(
            await sources.filter_export_eligible_sources(
                session,
                source_fences,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
        if eligible_source_ids != {item.source_id for item in source_fences}:
            return EntityExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        eligible = sources.export_eligible_source_ids(scope=scope)
        aliases = list(
            (
                await session.scalars(
                    select(EntityAlias)
                    .where(
                        EntityAlias.entity_id == fence.id,
                        or_(
                            EntityAlias.origin == "owner",
                            exists(
                                select(EntityAliasEvidence.id)
                                .join(
                                    EntityEvidenceMembership,
                                    EntityEvidenceMembership.id
                                    == EntityAliasEvidence.membership_id,
                                )
                                .where(
                                    EntityAliasEvidence.alias_id == EntityAlias.id,
                                    EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
                                    EntityEvidenceMembership.workspace_id == scope.workspace_id,
                                    EntityEvidenceMembership.source_id.in_(eligible),
                                )
                            ),
                        ),
                    )
                    .order_by(EntityAlias.id)
                    .limit(101)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        evidence = list(
            (
                await session.scalars(
                    select(EntityEvidenceMembership)
                    .where(
                        EntityEvidenceMembership.entity_id == fence.id,
                        EntityEvidenceMembership.workspace_id == scope.workspace_id,
                        EntityEvidenceMembership.source_id.in_(eligible),
                    )
                    .order_by(EntityEvidenceMembership.id)
                    .limit(101)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(aliases) > 100 or len(evidence) > 100:
            return EntityExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        alias_support_rows: dict[UUID, list[Any]] = {}
        for alias in aliases:
            support_rows = list(
                (
                    await session.execute(
                        select(EntityAliasEvidence, EntityEvidenceMembership)
                        .join(
                            EntityEvidenceMembership,
                            EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
                        )
                        .where(
                            EntityAliasEvidence.alias_id == alias.id,
                            EntityEvidenceMembership.entity_id == alias.entity_id,
                            EntityEvidenceMembership.workspace_id == scope.workspace_id,
                            EntityEvidenceMembership.source_id.in_(eligible),
                        )
                        .order_by(EntityAliasEvidence.id)
                        .limit(101)
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            if len(support_rows) > 100:
                return EntityExportFenceValidation(
                    valid=False, reason="record_changed", observed_snapshot_count=observed
                )
            alias_support_rows[alias.id] = support_rows
        if sum(map(len, alias_support_rows.values())) > 100:
            return EntityExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        source_ids = {item.source_id for item in evidence}
        source_ids.update(
            membership.source_id
            for rows_for_alias in alias_support_rows.values()
            for _, membership in rows_for_alias
        )
        if len(source_ids) > 100:
            return EntityExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        generations = await _entity_export_source_generations(
            session,
            source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        alias_values = [
            EntityExportAlias(
                id=alias.id,
                alias=alias.alias,
                confirmed=alias.confirmed,
                origin=alias.origin,
                confidence=alias.confidence,
                created_at=alias.created_at,
                supports=[
                    EntityExportAliasEvidence(
                        membership_id=membership.id,
                        source_id=membership.source_id,
                        source_generation=generations[membership.source_id],
                        confidence=support.confidence,
                    )
                    for support, membership in alias_support_rows[alias.id]
                ],
            )
            for alias in aliases
        ]
        evidence_values = [
            EntityExportEvidence(
                id=item.id,
                source_id=item.source_id,
                source_generation=generations[item.source_id],
                document_id=item.document_id,
                document_version_id=item.document_version_id,
                chunk_id=item.chunk_id,
                observed_at=item.observed_at,
                extracted_at=item.extracted_at,
                confidence=item.confidence,
            )
            for item in evidence
        ]
        alias_digest = sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in alias_values],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        evidence_digest = sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in evidence_values],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            [item.id for item in aliases] != fence.alias_ids
            or [item.id for item in evidence] != fence.evidence_ids
            or alias_digest != fence.alias_digest
            or evidence_digest != fence.evidence_digest
        ):
            return EntityExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return EntityExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)
