from __future__ import annotations

# Cross-module contract re-exports (importers use this public module, never private submodules).
from modules.knowledge.entities.schemas import (
    EvidenceRef,
)

# isort: split
import base64
import binascii
import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, delete, desc, exists, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_graph_change
from core.workspaces.admission import ALL_SCOPE_TYPES, admit_scope
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityExtractionResult,
    EntityExtractionWork,
    EntityFieldEvidence,
    EntityOwnerAction,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    AliasCreate,
    EntityAliasRead,
    EntityCreate,
    EntityEvidencePage,
    EntityEvidenceRead,
    EntityExportAlias,
    EntityExportAliasEvidence,
    EntityExportEvidence,
    EntityExportFence,
    EntityExportFenceValidation,
    EntityExportPage,
    EntityExportRead,
    EntityHistoryItem,
    EntityHistoryPage,
    EntityMembershipReferenceRead,
    EntityPage,
    EntityPatch,
    EntityRead,
    EntityReferenceRead,
    EntityRelationshipReviewRequest,
    EntityRelationshipReviewResult,
    EntityReviewAssignmentRequest,
    EntityReviewAssignmentResult,
    EntityReviewCandidate,
    EntityReviewEndpoint,
    EntityReviewEvidence,
    EntityReviewPage,
    EntitySourceExportFence,
    EntityTemporalNodeSeed,
    VersionMembershipReference,
    canonicalize_name,
    validate_metadata,
)
from modules.knowledge.entities.seed import (
    ensure_demo_entities,  # re-export: used by documents seed
)
from modules.sources import public as sources
from modules.sources.public import SourceExportFence, SourceFence


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before entity locks.

    Delegates to the canonical ``admit_scope``: typed workspace/admin-read/job scope (``TypeError``), owner-only
    members (403), admin viewers denied when ``lock`` is true (403), boolean multi-workspace flag, then a locked
    (with ``expected``) or read access fence. No commit.
    """
    return await admit_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=lock, expected=expected,
        allowed_scopes=ALL_SCOPE_TYPES, scope_label="entity workspace", admin_read_only_on_lock=True,
    )


async def observability_quality_summary(
    session: AsyncSession, *, instance_operator: bool,
) -> dict[str, int]:
    """Return global entity quality counts only for the explicit instance operator route."""
    if instance_operator is not True:
        raise ForbiddenError("Instance operator access required")
    unresolved = int(await session.scalar(select(func.count()).select_from(Entity).where(
        and_(Entity.canonical_name.is_(None), Entity.name.is_(None))
    )) or 0)
    failed_extraction = int(await session.scalar(select(func.count()).select_from(EntityExtractionWork).where(
        EntityExtractionWork.status == "failed"
    )) or 0)
    return {"unresolved_entities": unresolved, "failed_extraction": failed_extraction}


# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "EntityEvidencePage",
    "EntityHistoryPage",
    "EntityPage",
    "EntityRead",
    "EntityRelationshipReviewRequest",
    "EntityRelationshipReviewResult",
    "EntityReviewAssignmentRequest",
    "EntityReviewAssignmentResult",
    "EntityReviewPage",
    "EvidenceRef",
    "ensure_demo_entities",
    "validate_metadata",
]

ENTITY_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _encode_entity_export_cursor(owner_id: int, workspace_id: UUID, snapshot_at: datetime, position_at: datetime, position_id: UUID) -> str:
    """Encode an owner-, workspace- and snapshot-bound entity keyset position."""
    payload = json.dumps({"v": 1, "owner": owner_id, "workspace": str(workspace_id), "kind": "entities",
                          "snapshot": snapshot_at.astimezone(UTC).isoformat(),
                          "at": position_at.astimezone(UTC).isoformat(), "id": str(position_id)},
                         sort_keys=True, separators=(",", ":")).encode()
    return b64url_encode(payload)


def _decode_entity_export_cursor(cursor: str, owner_id: int, workspace_id: UUID) -> tuple[datetime, datetime, UUID]:
    """Decode a canonical bounded cursor, rejecting cross-owner and future snapshots."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"v", "owner", "workspace", "kind", "snapshot", "at", "id"}:
            raise ValueError
        if value["v"] != 1 or value["owner"] != owner_id or value["workspace"] != str(workspace_id) or value["kind"] != "entities":
            raise ValueError
        snapshot_at, position_at = datetime.fromisoformat(value["snapshot"]), datetime.fromisoformat(value["at"])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, position_at)):
            raise ValueError
        snapshot_at, position_at = snapshot_at.astimezone(UTC), position_at.astimezone(UTC)
        if snapshot_at > datetime.now(UTC):
            raise ValueError
        position_id = UUID(value["id"])
        if _encode_entity_export_cursor(owner_id, workspace_id, snapshot_at, position_at, position_id) != cursor:
            raise ValueError
        return snapshot_at, position_at, position_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid entity export cursor") from exc


def _entity_export_payload_bytes(items: list[EntityExportRead]) -> int:
    """Measure the exact compact UTF-8 JSON array returned for an entity page."""
    return len(json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


async def _entity_export_count(
    session: AsyncSession, snapshot_at: datetime, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Count cutoff-stable owner facts and entities backed by currently eligible source evidence."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    eligible_membership = exists(select(EntityEvidenceMembership.id).where(
        EntityEvidenceMembership.entity_id == Entity.id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.source_id.in_(eligible),
    ))
    eligible_alias_support = exists(select(EntityAliasEvidence.id).join(
        EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
    ).where(
        EntityAliasEvidence.alias_id == EntityAlias.id,
        EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.source_id.in_(eligible),
    ))
    owner_alias = exists(select(EntityAlias.id).where(
        EntityAlias.entity_id == Entity.id,
        or_(EntityAlias.origin == "owner", eligible_alias_support),
    ))
    return int(await session.scalar(select(func.count()).select_from(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        Entity.created_at <= snapshot_at, Entity.updated_at <= snapshot_at,
        or_(Entity.name_origin == "owner", Entity.description_origin == "owner", eligible_membership, owner_alias),
    )) or 0)


async def _entity_export_source_generations(
    session: AsyncSession, source_ids: set[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, int]:
    """Read current eligible source generations through the public lifecycle projection."""
    if not source_ids:
        return {}
    projection = sources.ingestion_lifecycle_projection(scope=scope).subquery()
    rows: Any = (await session.execute(select(projection.c.id, projection.c.generation).where(
        projection.c.id.in_(source_ids),
        projection.c.id.in_(sources.export_eligible_source_ids(scope=scope)),
    ))).all()
    generations = {source_id: int(generation) for source_id, generation in rows}
    if generations.keys() != source_ids:
        raise ValueError("Entity citation source is purging or no longer retained")
    return generations


async def _entity_export_field_is_supported(
    session: AsyncSession, entity: Entity, field_name: str, value: str | None, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Require one exact current-field support membership from an eligible source."""
    if value is None:
        return False
    value_hash = sha256(value.encode("utf-8")).hexdigest()
    return await session.scalar(select(EntityFieldEvidence.id).join(
        EntityEvidenceMembership, EntityEvidenceMembership.id == EntityFieldEvidence.membership_id,
    ).where(
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityFieldEvidence.entity_id == entity.id, EntityFieldEvidence.field_name == field_name,
        EntityFieldEvidence.value_hash == value_hash,
        EntityEvidenceMembership.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    ).limit(1)) is not None


def _entity_export_statement(
    scope: Scope, snapshot_at: datetime, position: tuple[datetime, UUID] | None, eligible: Any,
) -> Any:
    """Build the keyset select of exportable entities at the snapshot (owner-set fields or eligible support)."""
    eligible_membership = exists(select(EntityEvidenceMembership.id).where(
        EntityEvidenceMembership.entity_id == Entity.id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.source_id.in_(eligible),
    ))
    eligible_alias_support = exists(select(EntityAliasEvidence.id).join(
        EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
    ).where(
        EntityAliasEvidence.alias_id == EntityAlias.id,
        EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.source_id.in_(eligible),
    ))
    owner_alias = exists(select(EntityAlias.id).where(
        EntityAlias.entity_id == Entity.id,
        or_(EntityAlias.origin == "owner", eligible_alias_support),
    ))
    statement = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        Entity.created_at <= snapshot_at, Entity.updated_at <= snapshot_at,
        or_(Entity.name_origin == "owner", Entity.description_origin == "owner", eligible_membership, owner_alias),
    )
    if position is not None:
        statement = statement.where(tuple_(Entity.created_at, Entity.id) > position)
    return statement


async def _entity_export_children(
    session: AsyncSession, row: Entity, eligible: Any, scope: Scope,
) -> tuple[list[EntityAlias], list[EntityEvidenceMembership], dict[UUID, list[Any]]]:
    """Read one entity's exportable aliases, citations and per-alias support rows within explicit bounds.

    Raises ``ValueError`` when aliases or citations exceed 100, any alias exceeds 100 support rows, or total
    alias support exceeds 100, so no fact is silently truncated.
    """
    aliases = list((await session.scalars(select(EntityAlias).where(
        EntityAlias.entity_id == row.id,
        or_(EntityAlias.origin == "owner", exists(select(EntityAliasEvidence.id).join(
            EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
        ).where(
            EntityAliasEvidence.alias_id == EntityAlias.id,
            EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        ))),
    )
                                          .order_by(EntityAlias.id).limit(101)
                                          .execution_options(populate_existing=True))).all())
    evidence = list((await session.scalars(select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.entity_id == row.id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.source_id.in_(eligible),
    ).order_by(EntityEvidenceMembership.id).limit(101)
      .execution_options(populate_existing=True))).all())
    if len(aliases) > 100 or len(evidence) > 100:
        raise ValueError("An entity export record exceeds the alias or citation reference bound")
    alias_support_rows: dict[UUID, list[Any]] = {}
    for alias in aliases:
        support_rows = list((await session.execute(select(EntityAliasEvidence, EntityEvidenceMembership).join(
            EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
        ).where(
            EntityAliasEvidence.alias_id == alias.id,
            EntityEvidenceMembership.entity_id == alias.entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        ).order_by(EntityAliasEvidence.id).limit(101)
          .execution_options(populate_existing=True))).all())
        if len(support_rows) > 100:
            raise ValueError("An entity alias exceeds the support reference bound")
        alias_support_rows[alias.id] = support_rows
    if sum(map(len, alias_support_rows.values())) > 100:
        raise ValueError("An entity export record exceeds the total alias support bound")
    return aliases, evidence, alias_support_rows


async def _entity_export_item(
    session: AsyncSession, row: Entity, eligible: Any, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[EntityExportRead, EntityExportFence]:
    """Build one entity's export record and its final-validation fence.

    Non-owner name/description are exported only with exact eligible support (otherwise blanked with their
    origin). Reads children, distinct source generations (<= 100) and field support in that order.
    """
    aliases, evidence, alias_support_rows = await _entity_export_children(session, row, eligible, scope)
    source_ids = {item.source_id for item in evidence}
    source_ids.update(membership.source_id for rows_for_alias in alias_support_rows.values()
                      for _, membership in rows_for_alias)
    if len(source_ids) > 100:
        raise ValueError("An entity export record exceeds the distinct source generation bound")
    source_generations = await _entity_export_source_generations(
        session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    alias_items = [EntityExportAlias(
        id=alias.id, alias=alias.alias, confirmed=alias.confirmed, origin=alias.origin,
        confidence=alias.confidence, created_at=alias.created_at,
        supports=[EntityExportAliasEvidence(
            membership_id=membership.id, source_id=membership.source_id,
            source_generation=source_generations[membership.source_id], confidence=support.confidence,
        ) for support, membership in alias_support_rows[alias.id]],
    ) for alias in aliases]
    evidence_items = [EntityExportEvidence(
        id=item.id, source_id=item.source_id, source_generation=source_generations[item.source_id],
        document_id=item.document_id,
        document_version_id=item.document_version_id, chunk_id=item.chunk_id,
        observed_at=item.observed_at, extracted_at=item.extracted_at, confidence=item.confidence,
    ) for item in evidence]
    name = row.name
    canonical_name = row.canonical_name
    name_origin = row.name_origin
    if row.name_origin != "owner" and not await _entity_export_field_is_supported(
        session, row, "name", row.name, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        name = canonical_name = None
        name_origin = None
    description = row.description
    description_origin = row.description_origin
    if row.description_origin != "owner" and not await _entity_export_field_is_supported(
        session, row, "description", row.description,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        description = None
        description_origin = None
    item = EntityExportRead(
        id=row.id, type=row.type, name=name, canonical_name=canonical_name,
        description=description, revision=row.revision, name_origin=name_origin,
        description_origin=description_origin, first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at, created_at=row.created_at, updated_at=row.updated_at,
        aliases=alias_items, evidence=evidence_items,
    )
    fence = EntityExportFence(
        id=row.id, created_at=row.created_at, updated_at=row.updated_at, revision=row.revision,
        alias_ids=[alias.id for alias in alias_items], evidence_ids=[ev.id for ev in evidence_items],
        source_fences=[EntitySourceExportFence(source_id=source_id, generation=generation)
                       for source_id, generation in sorted(source_generations.items(), key=lambda pair: str(pair[0]))],
        alias_digest=sha256(json.dumps([alias.model_dump(mode="json") for alias in alias_items],
                                       ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        evidence_digest=sha256(json.dumps([ev.model_dump(mode="json") for ev in evidence_items],
                                          ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
    )
    return item, fence


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50,
    cursor: str | None = None, scope: Scope, multi_workspace_enabled: bool,
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
        snapshot_at, position_at, position_id = _decode_entity_export_cursor(cursor, owner_id, scope.workspace_id)
        position = (position_at, position_id)
    snapshot_count = await _entity_export_count(
        session, snapshot_at, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    eligible = sources.export_eligible_source_ids(scope=scope)
    statement = _entity_export_statement(scope, snapshot_at, position, eligible)
    rows = list((await session.scalars(statement.order_by(Entity.created_at, Entity.id)
                                       .limit(limit + 1).execution_options(populate_existing=True))).all())
    has_more = len(rows) > limit
    items: list[EntityExportRead] = []
    fences: list[EntityExportFence] = []
    for row in rows[:limit]:
        item, fence = await _entity_export_item(
            session, row, eligible, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        proposed = _entity_export_payload_bytes(items + [item])
        if proposed > ENTITY_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An entity export record exceeds the page byte budget")
            has_more = True
            break
        items.append(item)
        fences.append(fence)
    next_cursor = (_encode_entity_export_cursor(owner_id, scope.workspace_id, snapshot_at, items[-1].created_at, items[-1].id)
                   if has_more and items else None)
    return EntityExportPage(
        owner_id=owner_id, record_kind="entities", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=_entity_export_payload_bytes(items), max_payload_bytes=ENTITY_EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[EntityExportFence], scope: Scope,
    multi_workspace_enabled: bool,
) -> EntityExportFenceValidation:
    """Recheck the bounded entity set, revisions, aliases, and citation IDs before publication."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != _actor(scope) or record_kind != "entities" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Entity export revalidation input is invalid")
    observed = await _entity_export_count(
        session, snapshot_at, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if observed != expected_snapshot_count:
        return EntityExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Entity).where(
            Entity.id == fence.id, Entity.workspace_id == scope.workspace_id,
        ).execution_options(populate_existing=True))
        if row is None or (row.created_at, row.updated_at, row.revision) != (
            fence.created_at, fence.updated_at, fence.revision,
        ):
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        source_fences = [SourceExportFence(source_id=item.source_id, workspace_id=scope.workspace_id, generation=item.generation)
                         for item in fence.source_fences]
        eligible_source_ids = set(await sources.filter_export_eligible_sources(
            session, source_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ))
        if eligible_source_ids != {item.source_id for item in source_fences}:
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        eligible = sources.export_eligible_source_ids(scope=scope)
        aliases = list((await session.scalars(select(EntityAlias).where(
            EntityAlias.entity_id == fence.id,
            or_(EntityAlias.origin == "owner", exists(select(EntityAliasEvidence.id).join(
                EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
            ).where(
                EntityAliasEvidence.alias_id == EntityAlias.id,
                EntityEvidenceMembership.entity_id == EntityAlias.entity_id,
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                EntityEvidenceMembership.source_id.in_(eligible),
            ))),
        ).order_by(EntityAlias.id).limit(101).execution_options(populate_existing=True))).all())
        evidence = list((await session.scalars(select(EntityEvidenceMembership).where(
            EntityEvidenceMembership.entity_id == fence.id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.source_id.in_(eligible),
        ).order_by(EntityEvidenceMembership.id).limit(101)
          .execution_options(populate_existing=True))).all())
        if len(aliases) > 100 or len(evidence) > 100:
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        alias_support_rows: dict[UUID, list[Any]] = {}
        for alias in aliases:
            support_rows = list((await session.execute(select(EntityAliasEvidence, EntityEvidenceMembership).join(
                EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id,
            ).where(
                EntityAliasEvidence.alias_id == alias.id,
                EntityEvidenceMembership.entity_id == alias.entity_id,
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
                EntityEvidenceMembership.source_id.in_(eligible),
            ).order_by(EntityAliasEvidence.id).limit(101)
              .execution_options(populate_existing=True))).all())
            if len(support_rows) > 100:
                return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
            alias_support_rows[alias.id] = support_rows
        if sum(map(len, alias_support_rows.values())) > 100:
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        source_ids = {item.source_id for item in evidence}
        source_ids.update(membership.source_id for rows_for_alias in alias_support_rows.values()
                          for _, membership in rows_for_alias)
        if len(source_ids) > 100:
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        generations = await _entity_export_source_generations(
            session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        alias_values = [EntityExportAlias(
            id=alias.id, alias=alias.alias, confirmed=alias.confirmed, origin=alias.origin,
            confidence=alias.confidence, created_at=alias.created_at,
            supports=[EntityExportAliasEvidence(
                membership_id=membership.id, source_id=membership.source_id,
                source_generation=generations[membership.source_id], confidence=support.confidence,
            ) for support, membership in alias_support_rows[alias.id]],
        ) for alias in aliases]
        evidence_values = [EntityExportEvidence(
            id=item.id, source_id=item.source_id, source_generation=generations[item.source_id],
            document_id=item.document_id, document_version_id=item.document_version_id,
            chunk_id=item.chunk_id, observed_at=item.observed_at, extracted_at=item.extracted_at,
            confidence=item.confidence,
        ) for item in evidence]
        alias_digest = sha256(json.dumps([item.model_dump(mode="json") for item in alias_values],
                                         ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        evidence_digest = sha256(json.dumps([item.model_dump(mode="json") for item in evidence_values],
                                            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if ([item.id for item in aliases] != fence.alias_ids or [item.id for item in evidence] != fence.evidence_ids
                or alias_digest != fence.alias_digest or evidence_digest != fence.evidence_digest):
            return EntityExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return EntityExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)


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


async def _schedule_entity_change(
    session: AsyncSession, entity: Entity, fields: list[str], origin: str, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Flush workspace-local support and revision scheduling without committing."""
    from modules.knowledge.temporal import public as temporal
    rows = list((await session.execute(select(
        EntityEvidenceMembership.document_version_id, EntityEvidenceMembership.chunk_id,
    ).where(
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.entity_id == entity.id,
    ).limit(10_001))).all())
    if len(rows) > 10_000:
        raise ValueError("Entity change exceeds complete support bound")
    await temporal.schedule_canonical_change(
        session, kind="entity", canonical_id=entity.id, revision=entity.revision,
        fields=fields, support=[(version, chunk) for version, chunk in rows], origin=origin,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


class RedirectedEntityConflict(ValueError):
    """Signal that a write used an entity ID redirected by a merge."""


class TerminalEntityConflict(LookupError):
    """Signal that an entity identity was deleted and cannot be followed."""
if TYPE_CHECKING:
    from modules.knowledge.documents.public import ExtractionEvidenceRef
    from modules.knowledge.entities.schemas import EntityExtractionStatus as _ExtractionStatus


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


async def _aliases(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope,
) -> dict[UUID, list[EntityAlias]]:
    """Load ordered display aliases only from the admitted workspace."""
    if not entity_ids:
        return {}
    result: dict[UUID, list[EntityAlias]] = {}
    aliases = (await session.scalars(
        select(EntityAlias).join(Entity, Entity.id == EntityAlias.entity_id).where(
            EntityAlias.entity_id.in_(entity_ids), Entity.workspace_id == scope.workspace_id,
            or_(EntityAlias.origin.is_not(None), EntityAlias.source_id.is_(None)),
        ).order_by(EntityAlias.alias)
    )).all()
    for alias in aliases:
        result.setdefault(alias.entity_id, []).append(alias)
    return result


async def record_owner_action(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    operation: str,
    reason: str,
    affected_ids: list[UUID],
    revisions: dict[str, int | None] | None = None,
) -> None:
    """Queue an actor-attributed workspace correction record without committing."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    clean_reason = " ".join(reason.split())
    if not clean_reason or len(clean_reason) > 300:
        raise ValueError("Owner action reason must contain 1 to 300 characters")
    session.add(EntityOwnerAction(
        workspace_id=scope.workspace_id,
        actor_id=_actor(scope),
        operation=operation,
        reason=clean_reason,
        affected_ids=[str(identifier) for identifier in affected_ids],
        revisions=revisions or {},
        created_at=datetime.now(UTC),
    ))


async def list_entities(
    session: AsyncSession, limit: int, cursor: str | None, entity_type: str | None, query: str | None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityPage:
    """Return a bounded cursor page of canonical entities in one admitted workspace."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Entity page limit must be between 1 and 100")
    statement = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        ~Entity.id.in_(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
        )),
    )
    if entity_type:
        statement = statement.where(Entity.type == entity_type)
    if query:
        # Escape LIKE metacharacters so a literal % or _ in the query is not a wildcard.
        def _like_escape(value: str) -> str:
            """Escape backslash, ``%`` and ``_`` so user text matches literally under ``ILIKE ... ESCAPE '\\'``."""
            return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

        statement = statement.where(or_(
            Entity.name.ilike(f"%{_like_escape(query)}%", escape="\\"),
            Entity.canonical_name.ilike(f"%{_like_escape(canonicalize_name(query))}%", escape="\\"),
        ))
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Entity.created_at, Entity.id) < (created_at, identifier))
    rows = list((await session.scalars(
        statement.order_by(desc(Entity.created_at), desc(Entity.id)).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    aliases = await _aliases(session, [row.id for row in rows], scope=scope)
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return EntityPage(items=[_entity_read(row, aliases.get(row.id)) for row in rows], next_cursor=next_cursor)


async def get_entity(
    session: AsyncSession, entity_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Read a canonical entity through redirects, returning None when unavailable."""
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        return None
    entity = await session.scalar(select(Entity).where(
        Entity.id == canonical_id, Entity.workspace_id == scope.workspace_id,
    ))
    if entity is None:
        return None
    aliases = await _aliases(session, [entity.id], scope=scope)
    return _entity_read(entity, aliases.get(entity.id))


_MAX_CANONICAL_HOPS = 32


async def _resolve_canonical_bulk(
    session: AsyncSession, ids: list[UUID], *, workspace_id: UUID,
) -> dict[UUID, UUID | Exception]:
    """Resolve many entity IDs through the owner redirect chain with one query round per hop (no admission).

    Mirrors ``resolve_canonical_entity_id`` exactly, per ID: a redirect-free ID must exist in the workspace
    (else ``LookupError("Entity reference is missing")``), a tombstoned redirect (NULL target) yields
    ``LookupError("Entity identity was deleted")``, a revisited target ``ValueError`` (cycle) and a chain
    longer than 32 hops ``ValueError`` (limit). Failures are returned as exception *values* so callers keep
    the first-failure-in-input-order semantics of the sequential loop. At most 33 rounds x 2 statements.
    """
    unique = list(dict.fromkeys(ids))
    state = {identifier: identifier for identifier in unique}
    seen = {identifier: {identifier} for identifier in unique}
    result: dict[UUID, UUID | Exception] = {}
    pending = set(unique)
    for _ in range(_MAX_CANONICAL_HOPS):
        if not pending:
            break
        currents = {state[identifier] for identifier in pending}
        redirects = {
            old: target for old, target in (await session.execute(
                select(EntityRedirect.old_entity_id, EntityRedirect.target_entity_id).where(
                    EntityRedirect.workspace_id == workspace_id, EntityRedirect.old_entity_id.in_(currents),
                )
            )).all()
        }
        unredirected = currents - redirects.keys()
        existing = set((await session.scalars(select(Entity.id).where(
            Entity.id.in_(unredirected), Entity.workspace_id == workspace_id,
        ))).all()) if unredirected else set()
        for identifier in list(pending):
            current = state[identifier]
            if current not in redirects:
                result[identifier] = current if current in existing else LookupError("Entity reference is missing")
                pending.discard(identifier)
                continue
            target = redirects[current]
            if target is None:
                result[identifier] = LookupError("Entity identity was deleted")
                pending.discard(identifier)
            elif target in seen[identifier]:
                result[identifier] = ValueError("Entity redirect cycle detected")
                pending.discard(identifier)
            else:
                seen[identifier].add(target)
                state[identifier] = target
    for identifier in pending:
        result[identifier] = ValueError("Entity redirect chain exceeds its limit")
    return result


async def resolve_canonical_entity_ids(
    session: AsyncSession, ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID | None]:
    """Bulk ``resolve_canonical_entity_id``: canonical IDs aligned with ``ids`` (duplicates allowed, <=100).

    Read-only, one admission, a constant number of queries per redirect hop. IDs that are missing or
    deleted map to ``None`` (the singular raises ``LookupError``); a redirect cycle or over-long chain
    raises ``ValueError`` exactly like the singular, for the first such ID in input order.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 100:
        raise ValueError("Entity bulk query must contain at most 100 IDs")
    resolved = await _resolve_canonical_bulk(session, ids, workspace_id=scope.workspace_id)
    canonical: list[UUID | None] = []
    for identifier in ids:
        outcome = resolved[identifier]
        if isinstance(outcome, ValueError):
            raise outcome
        canonical.append(None if isinstance(outcome, Exception) else outcome)  # only LookupError remains here
    return canonical


async def get_entities(
    session: AsyncSession, ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[EntityRead | None]:
    """Bulk ``get_entity``: reads aligned with ``ids`` (duplicates allowed, at most 100), ``None`` if unavailable.

    Follows redirects, applies the same workspace scoping and builds each read with the same alias
    projection as ``get_entity``; costs a constant number of queries instead of ~4 per ID. Read-only.
    """
    canonical_ids = await resolve_canonical_entity_ids(
        session, ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    wanted = list(dict.fromkeys(identifier for identifier in canonical_ids if identifier is not None))
    if not wanted:
        return [None] * len(ids)
    rows = {row.id: row for row in (await session.scalars(select(Entity).where(
        Entity.id.in_(wanted), Entity.workspace_id == scope.workspace_id,
    ))).all()}
    aliases = await _aliases(session, list(rows), scope=scope)
    return [
        _entity_read(rows[identifier], aliases.get(identifier)) if identifier is not None and identifier in rows else None
        for identifier in canonical_ids
    ]


async def get_entity_refs(
    session: AsyncSession, ids: list[UUID], *, for_write: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> list[EntityReferenceRead]:
    """Resolve up to 100 unique entity IDs while preserving requested order.

    Read mode follows redirects. Write mode rejects redirected and terminally
    deleted IDs, then locks canonical rows in sorted order. Missing references
    raise LookupError; duplicate or oversized input raises ValueError.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValueError("Entity reference query must contain up to 100 unique IDs")
    if not ids:
        return []
    canonical_ids: dict[UUID, UUID] = {}
    if not for_write:
        # Read mode: one bulk redirect resolution (constant queries) instead of two queries per ID. The first
        # failing ID in request order raises the same exception the sequential loop below would raise.
        resolved = await _resolve_canonical_bulk(session, ids, workspace_id=scope.workspace_id)
        for identifier in ids:
            outcome = resolved[identifier]
            if isinstance(outcome, Exception):
                raise outcome
            canonical_ids[identifier] = outcome
    for identifier in ids if for_write else ():
        try:
            canonical_ids[identifier] = await resolve_canonical_entity_id(
                session, identifier, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
        except LookupError as exc:
            if for_write and await session.scalar(select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
                EntityRedirect.old_entity_id == identifier,
            )) is not None:
                raise TerminalEntityConflict("Entity identity was deleted") from exc
            raise
    if for_write and any(canonical_ids[identifier] != identifier for identifier in ids):
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    query = select(Entity).where(
        Entity.workspace_id == scope.workspace_id,
        Entity.id.in_(set(canonical_ids.values())),
    )
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


async def resolve_canonical_entity_id(
    session: AsyncSession, entity_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID:
    """Follow the bounded owner redirect chain; malformed cycles fail closed."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    current = entity_id
    seen = {current}
    for _ in range(32):
        redirect = await session.scalar(select(EntityRedirect).where(
            EntityRedirect.old_entity_id == current,
            EntityRedirect.workspace_id == scope.workspace_id,
        ))
        if redirect is None:
            if await session.scalar(select(Entity.id).where(
                Entity.id == current, Entity.workspace_id == scope.workspace_id,
            )) is None:
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
    session: AsyncSession, ids: list[UUID], *, for_write: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> list[EntityMembershipReferenceRead]:
    """Resolve unique memberships, acquiring stable entity/ID locks for writes."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 200 or len(set(ids)) != len(ids):
        raise ValueError("Entity membership query must contain up to 200 unique IDs")
    if not ids:
        return []
    query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.id.in_(ids),
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
    )
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
    session: AsyncSession, document_version_id: UUID, chunk_ids: list[UUID], *,
    scope: Scope, multi_workspace_enabled: bool,
) -> list[VersionMembershipReference]:
    """Return bounded canonical memberships for chunks already authorized by documents extraction input.

    The caller must keep the documents source/document egress fence and must
    validate each returned chunk against that extraction input. The detached
    result exposes membership keys for model selection; the model never chooses
    a global entity ID.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Version membership chunks must be unique and bounded")
    if not chunk_ids:
        return []
    rows = (await session.execute(
        select(EntityEvidenceMembership, Entity)
        .join(Entity, Entity.id == EntityEvidenceMembership.entity_id)
        .where(
            EntityEvidenceMembership.document_version_id == document_version_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            Entity.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.chunk_id.in_(chunk_ids),
            ~EntityEvidenceMembership.entity_id.in_(select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
            )),
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
    *, scope: Scope, multi_workspace_enabled: bool,
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
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(chunk_ids) > 100 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Retained membership chunks must be unique and bounded to100")
    if not chunk_ids:
        return []
    from modules.knowledge.documents import public as documents

    fences = await documents.review_version_fences(
        session, [document_version_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    fence = fences.get(document_version_id)
    if fence is None:
        raise LookupError("Retained membership version is unavailable")
    source = await sources.get_connector_source(
        session, fence.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.status != "active" or source.generation != fence.current_source_generation:
        raise LookupError("Retained membership source policy or generation changed")
    try:
        evidence = await documents.read_evidence_refs(
            session, [(document_version_id, chunk_id) for chunk_id in chunk_ids],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
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
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            Entity.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.chunk_id.in_(chunk_ids),
            ~EntityEvidenceMembership.entity_id.in_(select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
            )),
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
    session: AsyncSession, entity_id: UUID, limit: int = 50, cursor: str | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityEvidencePage | None:
    """Return evidence for a canonical entity with document-version provenance."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Entity evidence page limit must be between 1 and 100")
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        return None
    statement = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.entity_id == canonical_id,
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
    )
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
        session, list(dict.fromkeys((row.document_version_id, row.chunk_id) for row in rows)),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
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


async def list_entity_evidence_bulk(
    session: AsyncSession, ids: list[UUID], limit: int = 50,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[EntityEvidencePage | None]:
    """Bulk first-page ``list_entity_evidence``: pages aligned with ``ids`` (duplicates allowed, at most 100).

    Per entity the result equals ``list_entity_evidence(id, limit)`` without a cursor: redirects are followed,
    rows are ordered ``(extracted_at, id)`` and cut at ``limit`` (``next_cursor`` set when more exist), items
    whose evidence ref is no longer readable are dropped, and a missing entity yields ``None``. One
    window-function query selects the first ``limit + 1`` memberships of every entity and evidence refs are
    read in batches of at most 100 unique pairs, instead of ~3 statements per entity. Read-only.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Entity evidence page limit must be between 1 and 100")
    canonical_ids = await resolve_canonical_entity_ids(
        session, ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    wanted = list(dict.fromkeys(identifier for identifier in canonical_ids if identifier is not None))
    rows_by_entity: dict[UUID, list[EntityEvidenceMembership]] = {identifier: [] for identifier in wanted}
    if wanted:
        position = func.row_number().over(
            partition_by=EntityEvidenceMembership.entity_id,
            order_by=(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id),
        )
        ranked = select(EntityEvidenceMembership.id.label("membership_id"), position.label("position")).where(
            EntityEvidenceMembership.entity_id.in_(wanted),
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
        ).subquery()
        memberships = (await session.scalars(
            select(EntityEvidenceMembership)
            .join(ranked, ranked.c.membership_id == EntityEvidenceMembership.id)
            .where(ranked.c.position <= limit + 1)
            .order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id)
        )).all()
        for membership in memberships:
            rows_by_entity[membership.entity_id].append(membership)
    from modules.knowledge.documents import public as documents

    pairs = list(dict.fromkeys(
        (row.document_version_id, row.chunk_id)
        for identifier in wanted for row in rows_by_entity[identifier][:limit]
    ))
    by_pair: dict[tuple[UUID, UUID], Any] = {}
    for start in range(0, len(pairs), 100):  # read_evidence_refs accepts at most 100 unique pairs per call
        for evidence_ref in await documents.read_evidence_refs(
            session, pairs[start:start + 100], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            by_pair[(evidence_ref.document_version_id, evidence_ref.chunk_id)] = evidence_ref
    pages: dict[UUID, EntityEvidencePage] = {}
    for identifier in wanted:
        entity_rows = rows_by_entity[identifier]
        has_more = len(entity_rows) > limit
        entity_rows = entity_rows[:limit]
        items = [
            EntityEvidenceRead(
                id=row.id, entity_id=row.entity_id, document_id=ref.document_id,
                document_version_id=row.document_version_id, version_number=ref.version_number,
                chunk_id=row.chunk_id, observed_at=row.observed_at, extracted_at=row.extracted_at,
                confidence=row.confidence, source_id=ref.source_id, title=ref.title,
                canonical_url=ref.canonical_url,
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot, excerpt=ref.excerpt,
            )
            for row in entity_rows
            if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
        ]
        next_cursor = encode_cursor(entity_rows[-1].extracted_at, entity_rows[-1].id) if has_more and entity_rows else None
        pages[identifier] = EntityEvidencePage(items=items, next_cursor=next_cursor)
    return [pages[identifier] if identifier is not None else None for identifier in canonical_ids]


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


def _review_candidate_item(
    work: EntityExtractionWork, result: EntityExtractionResult, candidate: dict[str, Any], state: object, fences: dict[UUID, Any],
) -> tuple[EntityReviewCandidate, list[tuple[UUID, UUID]], tuple[UUID, UUID, str, str, UUID] | None] | None:
    """Project one stored review candidate (pure) into its DTO plus evidence and endpoint lookup keys.

    Returns ``None`` for malformed candidates (non-string name/reason). The item is actionable only for a
    succeeded work row with an immutable matching digest, a live version fence and the identity fields of its
    kind; malformed ids/chunk ids are silently skipped, evidence is capped at 5 chunks per entity candidate.
    """
    name, reason = candidate.get("candidate_name", candidate.get("candidate_name")), candidate.get("reason")
    if not isinstance(name, str) or not isinstance(reason, str):
        return None
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
    item = EntityReviewCandidate(
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
    )
    endpoint_key = (work.id, work.document_version_id, str(candidate.get("source_candidate_key")), str(candidate.get("target_candidate_key")), candidate_evidence[0][1]) if kind == "relationship" and candidate.get("source_candidate_key") and candidate.get("target_candidate_key") and candidate_evidence else None
    return item, candidate_evidence, endpoint_key


def _collect_review_items(
    rows: list[Any], fences: dict[UUID, Any], limit: int, cursor_work: UUID | None, cursor_index: int,
) -> tuple[list[EntityReviewCandidate], list[list[tuple[UUID, UUID]]], list[tuple[UUID, UUID, str, str, UUID] | None], str | None]:
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
            if isinstance(state, dict) and state.get("kind") in {"assigned", "resolved", "suppressed"}:
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
    endpoints: tuple[UUID, UUID, str, str, UUID], endpoint_rows: list[Any], entity_refs_by_id: dict[UUID, Any],
) -> list[EntityReviewEndpoint]:
    """Resolve a relationship candidate's source/target endpoint memberships to assigned/ambiguous/unassigned DTOs."""
    work_id, version_id, source_key, target_key, chunk_id = endpoints
    endpoint_dtos = []
    for key in (source_key, target_key):
        matches = [row for row in endpoint_rows if row.extraction_identity == str(work_id) and row.candidate_key == key and row.document_version_id == version_id and row.chunk_id == chunk_id]
        if len(matches) != 1:
            endpoint_dtos.append(EntityReviewEndpoint(state="ambiguous" if matches else "unassigned"))
            continue
        member = matches[0]
        endpoint_ref = entity_refs_by_id.get(member.entity_id)
        if endpoint_ref is None:
            endpoint_dtos.append(EntityReviewEndpoint(state="unassigned"))
            continue
        endpoint_dtos.append(EntityReviewEndpoint(
            state="assigned", entity_id=endpoint_ref.canonical_id, entity_name=endpoint_ref.name,
            entity_type=endpoint_ref.type, membership_id=member.id,
        ))
    return endpoint_dtos


async def _attach_review_evidence(
    session: AsyncSession, items: list[EntityReviewCandidate], evidence_keys: list[list[tuple[UUID, UUID]]],
    endpoint_keys: list[tuple[UUID, UUID, str, str, UUID] | None], fences: dict[UUID, Any], *,
    scope: Scope, multi_workspace_enabled: bool,
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
            session, all_keys[offset:offset + 100], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        refs_by_pair.update({(ref.document_version_id, ref.chunk_id): ref for ref in refs})
    endpoint_bindings = list(dict.fromkeys(
        (str(work_id), key, version_id, chunk_id)
        for endpoints in endpoint_keys if endpoints is not None
        for work_id, version_id, source_key, target_key, chunk_id in (endpoints,)
        for key in (source_key, target_key)
    ))
    endpoint_rows = list((await session.scalars(
        select(EntityEvidenceMembership).where(
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            tuple_(EntityEvidenceMembership.extraction_identity, EntityEvidenceMembership.candidate_key,
                   EntityEvidenceMembership.document_version_id, EntityEvidenceMembership.chunk_id).in_(endpoint_bindings)
        ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(401)
    )).all()) if endpoint_bindings else []
    endpoint_entities = sorted({row.entity_id for row in endpoint_rows}, key=str)
    entity_refs = []
    for offset in range(0, len(endpoint_entities), 100):
        entity_refs.extend(await get_entity_refs(
            session, endpoint_entities[offset:offset + 100], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ))
    entity_refs_by_id = {ref.requested_id: ref for ref in entity_refs}
    for item, keys, endpoints in zip(items, evidence_keys, endpoint_keys, strict=False):
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
        endpoint_dtos = _review_endpoint_dtos(endpoints, endpoint_rows, entity_refs_by_id)
        item.source_endpoint, item.target_endpoint = endpoint_dtos
        if (len(endpoint_dtos) != 2 or any(endpoint.state != "assigned" for endpoint in endpoint_dtos)
                or endpoint_dtos[0].entity_id == endpoint_dtos[1].entity_id):
            item.actionable = False
            item.snapshot_digest = None


async def list_review_candidates(
    session: AsyncSession, limit: int = 50, cursor: str | None = None, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntityReviewPage:
    """Bounded owner projection; only immutable future snapshots are actionable."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Review page limit must be between 1 and 100")
    cursor_time, cursor_work, cursor_index = _decode_review_cursor(cursor) if cursor else (None, None, 0)
    statement = select(EntityExtractionWork, EntityExtractionResult).join(
        EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id
    ).where(
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionResult.review_json.is_not(None),
    )
    if cursor_time is not None and cursor_work is not None:
        statement = statement.where(tuple_(EntityExtractionWork.updated_at, EntityExtractionWork.id) <= (cursor_time, cursor_work))
    rows = (await session.execute(
        statement
        .order_by(EntityExtractionWork.updated_at.desc(), EntityExtractionWork.id.desc())
        .limit(101)
    )).all()
    from modules.knowledge.documents import public as documents
    fences = await documents.review_version_fences(
        session, [work.document_version_id for work, _ in rows[:100]],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    items, evidence_keys, endpoint_keys, next_cursor = _collect_review_items(
        list(rows), fences, limit, cursor_work, cursor_index,
    )
    await _attach_review_evidence(
        session, items, evidence_keys, endpoint_keys, fences, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
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


def _review_entity_bindings(review: list[Any]) -> dict[str, tuple[str, set[UUID]]]:
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


async def _review_hints(
    session: AsyncSession, result_id: UUID, scope: Scope,
) -> tuple[EntityExtractionResult, EntityExtractionWork]:
    """Read (unlocked) the workspace-scoped extraction result and its work row for a review action.

    Raises ``LookupError`` when either row is unavailable. The rows are hints only; the caller re-reads and
    locks them FOR UPDATE after the owner Source and evidence locks.
    """
    result_hint = await session.scalar(select(EntityExtractionResult).join(
        EntityExtractionWork, EntityExtractionWork.id == EntityExtractionResult.work_id,
    ).where(
        EntityExtractionResult.id == result_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
    ))
    if result_hint is None:
        raise LookupError("Review result is unavailable")
    work_hint = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == result_hint.work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
    ))
    if work_hint is None:
        raise LookupError("Review work is unavailable")
    return result_hint, work_hint


async def _lock_review_owner_source(
    session: AsyncSession, work_hint: EntityExtractionWork, expected_generation: int, fence: AccessFence, *,
    scope: Scope, multi_workspace_enabled: bool, removed_message: str, changed_message: str,
) -> tuple[UUID, UUID, Any]:
    """Locate the reviewed version's document/source and lock the Source against the caller's access fence.

    Raises ``LookupError(removed_message)`` when the evidence locator is gone and
    ``ValueError(changed_message)`` when the Source is missing or its generation differs from
    ``expected_generation``. Returns ``(document_id, source_id, source_fence)``.
    """
    from modules.knowledge.documents import public as documents
    locator = await documents.review_version_locator(
        session, work_hint.document_version_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if locator is None:
        raise LookupError(removed_message)
    document_id, source_id = locator
    source = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=fence,
    )
    if source is None or source.generation != expected_generation:
        raise ValueError(changed_message)
    return document_id, source_id, source


async def _lock_review_extraction(
    session: AsyncSession, work_id: UUID, result_id: UUID, scope: Scope,
) -> tuple[EntityExtractionWork | None, EntityExtractionResult | None]:
    """Re-read the work row then the result row FOR UPDATE (that order) with fresh state (populate_existing)."""
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    result = await session.scalar(select(EntityExtractionResult).where(
        EntityExtractionResult.id == result_id, EntityExtractionResult.work_id == work_id,
    ).with_for_update().execution_options(populate_existing=True))
    return work, result


def _set_review_decision(result: EntityExtractionResult, candidate_id: UUID, state: dict[str, Any]) -> None:
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
    if not 1 <= len(chunks) <= 5 or len(set(chunks)) != len(chunks) or len(fingerprint) != 64 or len(candidate_key) != 64:
        raise ValueError("Review candidate evidence selector is invalid")
    return candidate_type, candidate_key, fingerprint, chunks, confidence


async def _record_assign_decisions(
    session: AsyncSession, membership_ids: list[UUID], payload: EntityReviewAssignmentRequest, fingerprint: str,
    actor_id: int, scope: Scope,
) -> None:
    """Add an ``assign`` correction decision per membership, locking any existing decision row first.

    Raises ``ValueError`` when a membership already has a conflicting owner decision. Adds rows to the
    session without flushing or committing.
    """
    for membership_id in membership_ids:
        existing = await session.scalar(select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.workspace_id == scope.workspace_id,
            EntityCorrectionDecision.scope == "evidence", EntityCorrectionDecision.membership_id == membership_id,
        ).with_for_update())
        if existing is None:
            session.add(EntityCorrectionDecision(
                workspace_id=scope.workspace_id,
                decision="assign", scope="evidence", entity_id=payload.target_entity_id,
                membership_id=membership_id, match_fingerprint=fingerprint, actor_id=actor_id,
                reason=payload.reason, created_at=datetime.now(UTC),
            ))
        elif existing.decision != "assign" or existing.entity_id != payload.target_entity_id:
            raise ValueError("Evidence membership already has a conflicting owner decision")


def _candidate_snapshots(review_json: object, candidate_id: UUID) -> list[dict[str, Any]]:
    """Return the review items whose ``candidate_id`` equals ``candidate_id`` (non-list review is empty)."""
    reviews = review_json if isinstance(review_json, list) else []
    return [item for item in reviews if isinstance(item, dict) and item.get("candidate_id") == str(candidate_id)]


def _assign_snapshot(
    result_hint: EntityExtractionResult, candidate_id: UUID, payload: EntityReviewAssignmentRequest,
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
    if snapshot.get("kind") != "entity" or snapshot.get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(snapshot) != payload.snapshot_digest:
        raise ValueError("Review candidate snapshot changed; reload the queue")
    if snapshot.get("decision_state"):
        raise ValueError("Review candidate was already resolved")
    if payload.future_document_id is not None:
        raise ValueError("Future document scope is not available from this review action")
    return reviews, snapshot


async def _assign_prior_and_target(
    session: AsyncSession, candidate_id: UUID, payload: EntityReviewAssignmentRequest, *, document_id: UUID,
    version_id: UUID, bindings: dict[str, tuple[str, set[UUID]]], candidate_type: str, scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[UUID, str, UUID | None] | None, Any]:
    """Read the prior correction decision and lock the target entity for write.

    Raises ``ValueError`` if a conflicting decision exists or the target's canonical id, type or revision
    differs from the request. Returns ``(prior_decision, target_ref)``.
    """
    prior = await get_document_correction_decisions(
        session, document_id, version_id, bindings,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    prior_decision = prior.get(str(candidate_id))
    if prior_decision is not None and (prior_decision[1] != "assign" or prior_decision[2] != payload.target_entity_id):
        raise ValueError("A conflicting correction decision already exists")
    refs = await get_entity_refs(
        session, [payload.target_entity_id], for_write=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    target = refs[0]
    if target.canonical_id != payload.target_entity_id or target.type != candidate_type or target.revision != payload.expected_target_revision:
        raise ValueError("Target entity changed or has an incompatible identity")
    return prior_decision, target


async def assign_review_candidate(
    session: AsyncSession, candidate_id: UUID, payload: EntityReviewAssignmentRequest, *,
    scope: Scope, multi_workspace_enabled: bool, actor_id: int,
) -> EntityReviewAssignmentResult:
    """Bind one durable extraction candidate to an owner-selected canonical entity.

    Lock order: access fence (admission) -> Source -> document evidence -> target entity -> extraction work
    and result rows (FOR UPDATE) -> evidence again -> correction decisions (FOR UPDATE). Every stale snapshot,
    selector or decision change raises ``ValueError``/``LookupError`` before any write; success commits once
    via ``commit_with_replay``.
    """
    from modules.knowledge.documents import public as documents

    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    if actor_id != _actor(scope):
        raise ForbiddenError("Review actor does not match the admitted owner")
    result_hint, work_hint = await _review_hints(session, payload.result_id, scope)
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    reviews, snapshot = _assign_snapshot(result_hint, candidate_id, payload)
    candidate_type, candidate_key, fingerprint, chunks, confidence = _assign_selector(snapshot)

    document_id, source_id, source = await _lock_review_owner_source(
        session, work_hint, payload.expected_owner_generation, fence, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, removed_message="Review evidence was removed",
        changed_message="Owner evidence fence changed; reload the review candidate",
    )
    evidence = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work_hint.document_version_id, source_generation=source.generation,
        chunk_ids=chunks, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")

    bindings = _review_entity_bindings(reviews)
    if str(candidate_id) not in bindings or bindings[str(candidate_id)] != (fingerprint, set(chunks)):
        raise ValueError("Selected candidate is missing from the complete result selector set")
    prior_decision, target = await _assign_prior_and_target(
        session, candidate_id, payload, document_id=document_id, version_id=work_hint.document_version_id,
        bindings=bindings, candidate_type=candidate_type, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )

    work, result = await _lock_review_extraction(session, work_hint.id, payload.result_id, scope)
    if work is None or result is None or work.status != "succeeded" or work.document_version_id != extraction_version_id or work.source_generation != extraction_generation or work.id != extraction_work_id:
        raise ValueError("Review extraction changed; reload the queue")
    current_snapshot = _candidate_snapshots(result.review_json, candidate_id)
    if len(current_snapshot) != 1 or current_snapshot[0].get("snapshot_digest") != payload.snapshot_digest or _review_snapshot_digest(current_snapshot[0]) != payload.snapshot_digest or current_snapshot[0].get("decision_state"):
        raise ValueError("Review candidate snapshot changed or was resolved")
    evidence = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work.document_version_id, source_generation=payload.expected_owner_generation,
        chunk_ids=chunks, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if evidence is None:
        raise LookupError("Selected source evidence is no longer retained")
    current_bindings = _review_entity_bindings(result.review_json if isinstance(result.review_json, list) else [])
    if current_bindings != bindings:
        raise ValueError("Same-result candidate selectors changed; reload the review")
    current = await get_document_correction_decisions(
        session, document_id, work.document_version_id, current_bindings, for_update=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
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
        observed_at=ref.observed_at, confidence=confidence, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ) for ref in evidence]
    await _record_assign_decisions(session, memberships, payload, fingerprint, actor_id, scope)
    _set_review_decision(result, candidate_id, {
        "kind": "assigned", "target_entity_id": str(payload.target_entity_id),
        "membership_ids": [str(value) for value in memberships],
    })
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_review_assign", reason=payload.reason,
        affected_ids=[payload.target_entity_id, candidate_id, *memberships],
        revisions={str(payload.target_entity_id): target.revision},
    )
    await session.flush()
    await commit_with_replay(
        session, [make_graph_change(entity_id=payload.target_entity_id, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return EntityReviewAssignmentResult(candidate_id=candidate_id, target_entity_id=payload.target_entity_id, membership_ids=memberships, revision=target.revision)


async def _relationship_endpoint_memberships(
    session: AsyncSession, *, scope: Scope, work_id: UUID, document_version_id: UUID, chunk_id: UUID,
    source_key: str, target_key: str,
) -> tuple[EntityEvidenceMembership, EntityEvidenceMembership]:
    """Require one distinct endpoint membership per candidate on the cited chunk.

    Reads at most 3 rows ordered by (entity_id, id); raises ``ValueError`` unless each candidate key has
    exactly one membership and the two resolve to different memberships and entities.
    """
    rows = list((await session.scalars(select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.extraction_identity == str(work_id),
        EntityEvidenceMembership.candidate_key.in_([source_key, target_key]),
        EntityEvidenceMembership.document_version_id == document_version_id,
        EntityEvidenceMembership.chunk_id == chunk_id,
    ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(3))).all())
    left = [row for row in rows if row.candidate_key == source_key]
    right = [row for row in rows if row.candidate_key == target_key]
    if len(left) != 1 or len(right) != 1 or left[0].id == right[0].id or left[0].entity_id == right[0].entity_id:
        raise ValueError("Both endpoints require one distinct exact evidence membership on the cited chunk")
    return left[0], right[0]


def _relationship_review_selector(
    result_hint: EntityExtractionResult, candidate_id: UUID, payload: EntityRelationshipReviewRequest,
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
    return snapshot, source_key, target_key, relationship_type, chunk_id


async def resolve_relationship_review(
    session: AsyncSession, candidate_id: UUID, payload: EntityRelationshipReviewRequest, *,
    scope: Scope, multi_workspace_enabled: bool, actor_id: int,
) -> EntityRelationshipReviewResult:
    """Publish a stored relationship only after both exact endpoint memberships exist.

    Lock order: access fence -> Source -> cited evidence -> endpoint entities -> endpoint memberships ->
    extraction work and result rows (FOR UPDATE); the endpoint memberships are re-read after the row locks
    and must be unchanged. Commits once via ``commit_with_replay``.
    """
    from modules.knowledge.documents import public as documents
    from modules.knowledge.relationships import public as relationships

    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    if actor_id != _actor(scope):
        raise ForbiddenError("Review actor does not match the admitted owner")
    result_hint, work_hint = await _review_hints(session, payload.result_id, scope)
    extraction_generation = work_hint.source_generation
    extraction_version_id = work_hint.document_version_id
    extraction_work_id = work_hint.id
    snapshot, source_key, target_key, relationship_type, chunk_id = _relationship_review_selector(
        result_hint, candidate_id, payload)

    document_id, source_id, source = await _lock_review_owner_source(
        session, work_hint, payload.expected_owner_generation, fence, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, removed_message="Relationship evidence was removed",
        changed_message="Owner evidence fence changed; reload the relationship review",
    )
    refs = await documents.lock_review_version_evidence(
        session, document_id=document_id, source_id=source_id,
        version_id=work_hint.document_version_id, source_generation=source.generation,
        chunk_ids=[chunk_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if refs is None:
        raise LookupError("Relationship evidence is no longer retained")

    async def endpoint_memberships() -> tuple[EntityEvidenceMembership, EntityEvidenceMembership]:
        """Bind the shared membership lookup to this review item."""
        return await _relationship_endpoint_memberships(
            session, scope=scope, work_id=work_hint.id, document_version_id=work_hint.document_version_id,
            chunk_id=chunk_id, source_key=source_key, target_key=target_key,
        )

    source_membership, target_membership = await endpoint_memberships()
    expected_membership_ids = (source_membership.id, target_membership.id)
    expected_membership_identity = {
        source_membership.id: (source_membership.entity_id, source_membership.document_version_id, source_membership.chunk_id),
        target_membership.id: (target_membership.entity_id, target_membership.document_version_id, target_membership.chunk_id),
    }
    endpoint_refs = await get_entity_refs(
        session, [source_membership.entity_id, target_membership.entity_id], for_write=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(endpoint_refs) != 2 or any(ref.requested_id != ref.canonical_id for ref in endpoint_refs):
        raise ValueError("Relationship endpoint identity changed; review the assignments")
    locked = await get_membership_refs(
        session, [source_membership.id, target_membership.id], for_write=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    work, result = await _lock_review_extraction(session, work_hint.id, payload.result_id, scope)
    if work is None or result is None or work.status != "succeeded" or work.source_generation != extraction_generation or work.document_version_id != extraction_version_id or work.id != extraction_work_id:
        raise ValueError("Relationship extraction changed; reload the queue")
    current = _candidate_snapshots(result.review_json, candidate_id)
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
        confidence=float(snapshot["confidence"]), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if relationship_id is None:
        raise ValueError("Relationship endpoints are identical")
    _set_review_decision(result, candidate_id, {"kind": "resolved", "relationship_id": str(relationship_id)})
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="relationship_review_resolve", reason=payload.reason,
        affected_ids=[relationship_id, candidate_id, source_membership.id, target_membership.id],
    )
    await session.flush()
    await commit_with_replay(
        session, [make_graph_change(relationship_id=relationship_id, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return EntityRelationshipReviewResult(candidate_id=candidate_id, relationship_id=relationship_id)


async def publish_derived_field(
    session: AsyncSession,
    *,
    entity_id: UUID,
    membership_id: UUID,
    field_name: Literal["name", "description"],
    value: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Publish a derived field and bind its exact value to one valid membership.

    The caller owns the source/document locks and the outer transaction. This
    command takes entity then membership locks and queues temporal desired state
    with exact current support in the same transaction; it never commits.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if field_name not in {"name", "description"}:
        raise ValueError("Unsupported derived entity field")
    entity = await session.scalar(
        select(Entity).where(Entity.id == entity_id, Entity.workspace_id == scope.workspace_id).with_for_update()
    )
    if entity is None:
        raise LookupError("Entity is missing")
    membership = await session.scalar(
        select(EntityEvidenceMembership)
        .where(
            EntityEvidenceMembership.id == membership_id,
            EntityEvidenceMembership.entity_id == entity_id,
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
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
        await _schedule_entity_change(
            session, entity, [field_name, "support"], "derived", scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return True


async def schedule_extraction_work(
    session: AsyncSession, document_version_id: UUID, source_generation: int,
    extractor_version: str, prompt_version: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityExtractionWork:
    """Create or lock durable extraction work keyed by version and prompt identity."""
    if len(extractor_version) > 64 or len(prompt_version) > 64:
        raise ValueError("Extraction versions are too long")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await session.execute(pg_insert(EntityExtractionWork).values(
        workspace_id=scope.workspace_id,
        document_version_id=document_version_id,
        source_generation=source_generation,
        extractor_version=extractor_version,
        prompt_version=prompt_version,
    ).on_conflict_do_nothing(constraint="uq_entity_extraction_work_identity"))
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.workspace_id == scope.workspace_id,
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
    session: AsyncSession, work_id: UUID, lease_owner: str, now: datetime, *, scope: Scope,
    multi_workspace_enabled: bool, lease_seconds: int = 110,
) -> EntityExtractionWork | None:
    """Claim due work under a lease of ``lease_seconds`` (callers pass one that outlives the model call), enforcing expiry and the five-attempt ceiling."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.workspace_id == scope.workspace_id,
    ).with_for_update())
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
    work.lease_expires_at = now + timedelta(seconds=lease_seconds)
    work.error_code = None
    await session.flush()
    return work


async def list_recoverable_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Lock and return bounded pending or expired extraction work IDs."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction recovery limit must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    now = datetime.now(UTC)
    return list((await session.scalars(
        select(EntityExtractionWork.id).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionWork.next_attempt_at <= now,
            or_(
                EntityExtractionWork.status == "pending",
                (EntityExtractionWork.status == "running") & (EntityExtractionWork.lease_expires_at <= now),
            ),
        ).order_by(EntityExtractionWork.next_attempt_at, EntityExtractionWork.created_at)
        .limit(limit).with_for_update(skip_locked=True)
    )).all())


async def terminalize_exhausted_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Fail expired running work at the attempt ceiling and clear its lease."""
    if not 1 <= limit <= 100:
        raise ValueError("Extraction terminalization limit must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    now = datetime.now(UTC)
    rows = list((await session.scalars(
        select(EntityExtractionWork).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
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


async def list_blocked_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID, int, str | None, str | None]]:
    """List due policy/capability-blocked work with its dependency fingerprint."""
    if not 1 <= limit <= 100:
        raise ValueError("Blocked extraction page size must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = (await session.execute(
        select(
            EntityExtractionWork.id, EntityExtractionWork.document_version_id,
            EntityExtractionWork.source_generation, EntityExtractionWork.error_code,
            EntityExtractionWork.dependency_fingerprint,
        ).where(
            EntityExtractionWork.workspace_id == scope.workspace_id,
            EntityExtractionWork.status == "blocked",
            EntityExtractionWork.error_code.in_(("ai_policy_denied", "structured_unsupported")),
            EntityExtractionWork.next_attempt_at <= datetime.now(UTC),
        ).order_by(EntityExtractionWork.updated_at, EntityExtractionWork.id).limit(limit)
    )).all()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


async def requeue_blocked_extraction_work(
    session: AsyncSession, work_id: UUID, previous_fingerprint: str | None, current_fingerprint: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Requeue blocked work only after its dependency fingerprint has changed."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if previous_fingerprint is None or previous_fingerprint == current_fingerprint:
        return False
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
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
    session: AsyncSession, work_id: UUID, fingerprint: str, *, scope: Scope,
    multi_workspace_enabled: bool, minutes: int = 15,
) -> None:
    """Delay a matching blocked work item's next dependency recheck."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id,
        EntityExtractionWork.workspace_id == scope.workspace_id,
        EntityExtractionWork.status == "blocked",
        EntityExtractionWork.dependency_fingerprint == fingerprint,
    ).with_for_update())
    if work is not None:
        work.next_attempt_at = datetime.now(UTC) + timedelta(minutes=minutes)


async def get_extraction_status(
    session: AsyncSession, document_version_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> _ExtractionStatus | None:
    """Return the newest work status and stored facts for one document version."""
    from modules.knowledge.entities.schemas import EntityExtractionStatus

    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(EntityExtractionWork, EntityExtractionResult)
        .outerjoin(EntityExtractionResult, EntityExtractionResult.work_id == EntityExtractionWork.id)
        .where(EntityExtractionWork.document_version_id == document_version_id,
               EntityExtractionWork.workspace_id == scope.workspace_id)
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
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Persist results and succeed only while the caller still owns a live lease."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.workspace_id == scope.workspace_id,
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
    scope: Scope, multi_workspace_enabled: bool,
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
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    work = await session.scalar(select(EntityExtractionWork).where(
        EntityExtractionWork.id == work_id, EntityExtractionWork.status == "running",
        EntityExtractionWork.workspace_id == scope.workspace_id,
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
    session: AsyncSession, entity_type: str, candidate_names: list[str], limit: int = 1000, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[dict[str, object]], bool]:
    """Return bounded same-type entities and exact confirmed aliases for resolution."""
    if not 1 <= limit <= 1000 or not 1 <= len(candidate_names) <= 30:
        raise ValueError("Resolution context must be bounded")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    normalized_names = sorted({canonicalize_name(name) for name in candidate_names})
    rows = list((await session.execute(
        select(Entity.id, Entity.type, Entity.name, Entity.revision)
        .where(
            Entity.workspace_id == scope.workspace_id,
            Entity.type == entity_type,
            ~Entity.id.in_(select(EntityRedirect.old_entity_id).where(
                EntityRedirect.workspace_id == scope.workspace_id,
            )),
        ).order_by(Entity.id).limit(limit + 1)
    )).all())
    overflow = len(rows) > limit
    rows = rows[:limit]
    aliases = (await session.execute(
        select(EntityAlias.entity_id, EntityAlias.normalized_alias).join(
            Entity, Entity.id == EntityAlias.entity_id,
        )
        .where(
            Entity.workspace_id == scope.workspace_id,
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


async def create_extracted_entity(
    session: AsyncSession, entity_type: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID:
    """Create an unnamed derived entity inside an already admitted workspace."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    entity = Entity(
        workspace_id=scope.workspace_id, type=entity_type, name=None, canonical_name=None,
        name_origin=None, description_origin=None,
    )
    session.add(entity)
    await session.flush()
    return entity.id


async def find_extraction_entity(
    session: AsyncSession, *, extraction_identity: str, candidate_key: str,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Return the canonical entity already bound to a deterministic extraction key, if any.

    Deterministic mappers (for example GitHub) use this with
    ``record_extraction_membership`` to stay idempotent: the first membership
    creates the entity, later calls find it. The earliest membership wins so
    the answer is stable; a merged entity is followed to its canonical ID. The
    caller holds the source lock that serializes find-or-create.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    entity_id = await session.scalar(
        select(EntityEvidenceMembership.entity_id).where(
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.extraction_identity == extraction_identity,
            EntityEvidenceMembership.candidate_key == candidate_key,
        ).order_by(EntityEvidenceMembership.extracted_at, EntityEvidenceMembership.id).limit(1)
    )
    if entity_id is None:
        return None
    try:
        return await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError:
        # The owner deleted the entity: report "absent" so the mapper recreates it
        # deterministically instead of failing every later record of the source.
        return None


async def record_extraction_membership(
    session: AsyncSession, *, entity_id: UUID, evidence_ref: ExtractionEvidenceRef,
    source_generation: int, extraction_identity: str, candidate_key: str,
    match_fingerprint: str, observed_at: datetime, confidence: float,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID:
    """Insert idempotent evidence membership after validating source generation and identity."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        not extraction_identity or len(extraction_identity) > 256
        or not candidate_key or len(candidate_key) > 256
        or len(match_fingerprint) != 64
        or not math.isfinite(confidence) or not 0 <= confidence <= 1
        or evidence_ref.source_generation != source_generation
    ):
        raise ValueError("Extraction membership values are invalid")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        raise LookupError("Extraction entity is missing")
    inserted = (await session.execute(pg_insert(EntityEvidenceMembership).values(
        workspace_id=scope.workspace_id,
        entity_id=entity_id, document_id=evidence_ref.document_id, source_id=evidence_ref.source_id,
        document_version_id=evidence_ref.document_version_id, chunk_id=evidence_ref.chunk_id,
        extraction_identity=extraction_identity, candidate_key=candidate_key,
        match_fingerprint=match_fingerprint,
        observed_at=observed_at, confidence=confidence,
    ).on_conflict_do_nothing(constraint="uq_entity_evidence_retry").returning(
        EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id,
    ))).first()
    if inserted is None:
        # Retry: the row already exists (nothing returned), so read back its id and owning entity.
        inserted = (await session.execute(select(
            EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id,
        ).where(
            EntityEvidenceMembership.workspace_id == scope.workspace_id,
            EntityEvidenceMembership.extraction_identity == extraction_identity,
            EntityEvidenceMembership.candidate_key == candidate_key,
            EntityEvidenceMembership.chunk_id == evidence_ref.chunk_id,
        ))).first()
    if inserted is None:
        raise RuntimeError("Entity evidence membership could not be recorded")
    membership_id, member_entity_id = inserted
    if member_entity_id != entity_id:
        raise ValueError("Extraction retry identity resolved to a different entity")
    return membership_id


async def _read_decision_rows(
    session: AsyncSession, *, scope: Scope, document_id: UUID, document_version_id: UUID,
    fingerprints: set[str], chunk_ids: set[UUID], for_update: bool,
) -> tuple[
    list[EntityEvidenceMembership], list[EntityCorrectionDecision],
    list[tuple[EntityCorrectionDecision, UUID, str | None]], list[EntityCorrectionDecision],
]:
    """Read matching memberships, evidence-scoped decisions and document-scoped decisions (each bounded to 201).

    Query order is memberships -> evidence decisions (skipped when no memberships) -> document decisions;
    with ``for_update`` each query takes ``FOR UPDATE`` in that order. Returns the memberships, the evidence
    decisions, the evidence decisions paired with their membership chunk/fingerprint, and the document rows.
    """
    membership_query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
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
    membership_ids = sorted({row.id for row in memberships}, key=str)
    decisions_query = select(EntityCorrectionDecision).where(
        EntityCorrectionDecision.workspace_id == scope.workspace_id,
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
            EntityCorrectionDecision.workspace_id == scope.workspace_id,
            EntityCorrectionDecision.scope == "document",
            EntityCorrectionDecision.document_id == document_id,
            EntityCorrectionDecision.match_fingerprint.in_(fingerprints),
        ).order_by(EntityCorrectionDecision.id).limit(201)
    if for_update:
        document_query = document_query.with_for_update()
    document_rows = list((await session.scalars(
        document_query.execution_options(populate_existing=True)
    )).all())
    return memberships, evidence_decisions, evidence_rows, document_rows


def _resolve_correction_decisions(
    candidates: dict[str, tuple[str, set[UUID]]], memberships: list[EntityEvidenceMembership],
    evidence_decisions: list[EntityCorrectionDecision],
    evidence_rows: list[tuple[EntityCorrectionDecision, UUID, str | None]],
    document_rows: list[EntityCorrectionDecision],
) -> dict[str, tuple[UUID, str, UUID | None] | None]:
    """Fold the read rows into one decision per candidate (pure).

    A candidate resolves to its newest matching decision, ``None`` when there is none, or a ``conflict``
    (nil-UUID id when nothing matched) when rows hit a read bound, evidence is partial or fingerprint-ambiguous,
    selectors overlap between candidates, or evidence and document decisions disagree.
    """
    memberships_by_selector: dict[tuple[str, UUID], list[EntityEvidenceMembership]] = {}
    for membership in memberships:
        if membership.match_fingerprint is not None:
            memberships_by_selector.setdefault(
                (membership.match_fingerprint, membership.chunk_id), []
            ).append(membership)
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


async def get_document_correction_decisions(
    session: AsyncSession, document_id: UUID, document_version_id: UUID,
    candidates: dict[str, tuple[str, set[UUID]]],
    *, scope: Scope, multi_workspace_enabled: bool, for_update: bool = False,
) -> dict[str, tuple[UUID, str, UUID | None] | None]:
    """Resolve bounded evidence/document owner decisions, reporting ambiguous conflicts.

    Admits the scope (read fence), enforces at most 30 candidates and 200 distinct chunks (``ValueError``),
    then reads rows (``FOR UPDATE`` when requested) and resolves each candidate; see
    ``_resolve_correction_decisions`` for the conflict rules.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(candidates) > 30:
        raise ValueError("Correction decision lookup exceeds its candidate limit")
    if not candidates:
        return {}
    fingerprints = {spec[0] for spec in candidates.values()}
    chunk_ids = {chunk_id for _, chunks in candidates.values() for chunk_id in chunks}
    if len(chunk_ids) > 200 or any(not chunks for _, chunks in candidates.values()):
        raise ValueError("Correction evidence binding exceeds its chunk limit")
    memberships, evidence_decisions, evidence_rows, document_rows = await _read_decision_rows(
        session, scope=scope, document_id=document_id, document_version_id=document_version_id,
        fingerprints=fingerprints, chunk_ids=chunk_ids, for_update=for_update,
    )
    return _resolve_correction_decisions(candidates, memberships, evidence_decisions, evidence_rows, document_rows)


async def create_entity(
    session: AsyncSession, payload: EntityCreate, *, scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead:
    """Create an owner-authored entity and aliases inside one admitted workspace transaction."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    entity = Entity(
        workspace_id=scope.workspace_id,
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
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            operation="entity_create", reason=payload.reason,
            affected_ids=[entity.id], revisions={str(entity.id): 1},
        )
        await _schedule_entity_change(
            session, entity, ["name", "description", "metadata", "aliases"], "owner",
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        await commit_with_replay(
            session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        )
    except IntegrityError:
        await session.rollback()
        raise
    return result


async def update_entity(
    session: AsyncSession, entity_id: UUID, payload: EntityPatch, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Apply a canonical owner-field update with revision and redirect fences.

    Admission is captured before canonical/entity locks. Returns None if the row
    disappears, rejects merged/deleted IDs with typed conflicts and stale revisions,
    marks edited fields
    owner-authored, removes their derived field support, then commits audit and
    graph changes plus exact-support temporal desired state in one transaction.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
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
            EntityFieldEvidence.membership_id.in_(select(EntityEvidenceMembership.id).where(
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
            )),
        ))
    if "description" in payload.model_fields_set:
        entity.description = payload.description
        entity.description_origin = "owner"
        await session.execute(delete(EntityFieldEvidence).where(
            EntityFieldEvidence.entity_id == entity_id,
            EntityFieldEvidence.field_name == "description",
            EntityFieldEvidence.membership_id.in_(select(EntityEvidenceMembership.id).where(
                EntityEvidenceMembership.workspace_id == scope.workspace_id,
            )),
        ))
    if "metadata" in payload.model_fields_set and payload.metadata is not None:
        entity.metadata_json = payload.metadata
    entity.revision += 1
    await session.flush()
    await session.refresh(entity)
    aliases = await _aliases(session, [entity.id], scope=scope)
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="entity_update", reason=payload.reason,
        affected_ids=[entity.id], revisions={str(entity.id): previous_revision},
    )
    await _schedule_entity_change(
        session, entity, sorted(payload.model_fields_set - {"expected_revision", "reason"}), "owner",
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return result


async def add_alias(
    session: AsyncSession, entity_id: UUID, payload: AliasCreate, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntityRead | None:
    """Add an owner-authored alias to a canonical entity and commit its audit.

    Admission and its access fence precede row locks. Redirected or terminal IDs
    raise typed conflicts; a missing canonical row returns None. A successful
    insert records the actor/reason and atomically schedules graph desired state.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
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
    aliases = await _aliases(session, [entity.id], scope=scope)
    result = _entity_read(entity, aliases.get(entity.id))
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="alias_create", reason=payload.reason,
        affected_ids=[entity.id, alias.id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(
        session, entity, ["aliases"], "owner", scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return result


async def delete_alias(
    session: AsyncSession, entity_id: UUID, alias_id: UUID, *, reason: str = "owner_alias_delete",
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Delete one alias from a canonical entity and commit its owner audit.

    The admission fence precedes canonical/entity/alias locks. Redirected or
    terminal IDs raise typed conflicts; a missing entity or alias returns False. Success
    records the actor/reason and atomically schedules graph desired state.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    try:
        canonical_id = await resolve_canonical_entity_id(
            session, entity_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    except LookupError as exc:
        if await session.scalar(select(EntityRedirect.old_entity_id).where(
            EntityRedirect.workspace_id == scope.workspace_id,
            EntityRedirect.old_entity_id == entity_id,
        )) is not None:
            raise TerminalEntityConflict("Entity identity was deleted") from exc
        raise
    if canonical_id != entity_id:
        raise RedirectedEntityConflict("Entity ID was merged; use its canonical ID")
    entity = await session.scalar(select(Entity).where(
        Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
    ).with_for_update())
    if entity is None:
        return False
    alias = await session.scalar(
        select(EntityAlias).where(
            EntityAlias.id == alias_id, EntityAlias.entity_id == entity_id,
        ).with_for_update()
    )
    if alias is None:
        return False
    await session.delete(alias)
    await record_owner_action(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        operation="alias_delete", reason=reason,
        affected_ids=[entity.id, alias_id], revisions={str(entity.id): entity.revision},
    )
    await _schedule_entity_change(
        session, entity, ["aliases"], "owner", scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session, [make_graph_change(entity_id=entity.id, scope=scope)], scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    )
    return True


async def delete_entity(
    session: AsyncSession, entity_id: UUID, *, reason: str = "owner_entity_delete",
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Delegate canonical deletion and its support cleanup to the correction owner."""
    from modules.knowledge.entities.corrections import delete_canonical_entity

    return await delete_canonical_entity(
        session, entity_id, reason=reason, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


@dataclass(frozen=True)
class EntitySupportClosure:
    """Workspace-qualified ID closure of entity support for one Source or Document."""

    source_id: UUID
    document_id: UUID | None
    membership_ids: tuple[UUID, ...]
    alias_ids: tuple[UUID, ...]
    alias_evidence_ids: tuple[UUID, ...]
    field_evidence_ids: tuple[UUID, ...]
    entity_ids: tuple[UUID, ...]
    overflow: bool


_CLEANUP_LIMIT = 10_000


def _require_cleanup_fences(
    actual: AccessFence, *, source_id: UUID, scope: Scope,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Require the live access fence and Source to still match those held by a cleanup transaction.

    Raises HTTP 409 "Cleanup authority changed" (kept as ``HTTPException``: purge callers in sources/documents/
    ingestion branch on ``HTTPException`` status codes) when the fence differs or the Source id/workspace does not
    match the closure. Pure comparison; no I/O.
    """
    if (
        actual != access_fence or source_fence.id != source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise HTTPException(status_code=409, detail="Cleanup authority changed")


async def support_cleanup_ids(
    session: AsyncSession, *, source_id: UUID, document_id: UUID | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> EntitySupportClosure:
    """Discover (nonlocking, ID-only) the entity support rows of one Source or Document."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    workspace_id = scope.workspace_id
    overflow = False
    member_where = [
        EntityEvidenceMembership.workspace_id == workspace_id,
        EntityEvidenceMembership.source_id == source_id,
    ]
    if document_id is not None:
        member_where.append(EntityEvidenceMembership.document_id == document_id)
    members = list((await session.execute(
        select(EntityEvidenceMembership.id, EntityEvidenceMembership.entity_id)
        .where(*member_where).order_by(EntityEvidenceMembership.id).limit(_CLEANUP_LIMIT + 1)
    )).all())
    overflow |= len(members) > _CLEANUP_LIMIT
    members = members[:_CLEANUP_LIMIT]
    membership_ids = [row[0] for row in members]
    entity_ids = {row[1] for row in members}
    alias_ids: set[UUID] = set()
    alias_evidence_ids: list[UUID] = []
    field_evidence_ids: list[UUID] = []
    if membership_ids:
        alias_support = list((await session.execute(
            select(EntityAliasEvidence.id, EntityAliasEvidence.alias_id)
            .join(EntityEvidenceMembership, EntityEvidenceMembership.id == EntityAliasEvidence.membership_id)
            .where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityAliasEvidence.membership_id.in_(membership_ids),
            ).order_by(EntityAliasEvidence.id).limit(_CLEANUP_LIMIT + 1)
        )).all())
        overflow |= len(alias_support) > _CLEANUP_LIMIT
        alias_support = alias_support[:_CLEANUP_LIMIT]
        alias_evidence_ids = [row[0] for row in alias_support]
        alias_ids.update(row[1] for row in alias_support)
        field_evidence_ids = list((await session.scalars(
            select(EntityFieldEvidence.id)
            .join(EntityEvidenceMembership, EntityEvidenceMembership.id == EntityFieldEvidence.membership_id)
            .where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityFieldEvidence.membership_id.in_(membership_ids),
            ).order_by(EntityFieldEvidence.id).limit(_CLEANUP_LIMIT + 1)
        )).all())
        overflow |= len(field_evidence_ids) > _CLEANUP_LIMIT
        field_evidence_ids = field_evidence_ids[:_CLEANUP_LIMIT]
    alias_clauses = [EntityAlias.source_id == source_id] if document_id is None else []
    if alias_ids:
        alias_clauses.append(EntityAlias.id.in_(alias_ids))
    if alias_clauses:
        aliases = list((await session.execute(
            select(EntityAlias.id, EntityAlias.entity_id)
            .join(Entity, Entity.id == EntityAlias.entity_id)
            .where(Entity.workspace_id == workspace_id, or_(*alias_clauses))
            .order_by(EntityAlias.id).limit(_CLEANUP_LIMIT + 1)
        )).all())
        overflow |= len(aliases) > _CLEANUP_LIMIT
        for alias_id, alias_entity_id in aliases[:_CLEANUP_LIMIT]:
            alias_ids.add(alias_id)
            entity_ids.add(alias_entity_id)
    return EntitySupportClosure(
        source_id=source_id, document_id=document_id,
        membership_ids=tuple(sorted(membership_ids)), alias_ids=tuple(sorted(alias_ids)),
        alias_evidence_ids=tuple(sorted(alias_evidence_ids)),
        field_evidence_ids=tuple(sorted(field_evidence_ids)),
        entity_ids=tuple(sorted(entity_ids)), overflow=overflow,
    )


async def prepare_support_cleanup_in_uow(
    session: AsyncSession, closure: EntitySupportClosure, *, entity_ids: tuple[UUID, ...],
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Lock the combined entity union, then support children, in UUID order; no mutation."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        source_id=closure.source_id, scope=scope, access_fence=access_fence, source_fence=source_fence,
    )
    if closure.overflow:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    workspace_id = scope.workspace_id
    locked_entities = sorted(set(entity_ids) | set(closure.entity_ids))
    workspace_entities = select(Entity.id).where(Entity.workspace_id == workspace_id)
    workspace_memberships = select(EntityEvidenceMembership.id).where(
        EntityEvidenceMembership.workspace_id == workspace_id,
    )
    if locked_entities:
        await session.scalars(
            select(Entity.id).where(Entity.workspace_id == workspace_id, Entity.id.in_(locked_entities))
            .order_by(Entity.id).with_for_update()
        )
    if closure.membership_ids:
        await session.scalars(
            select(EntityEvidenceMembership.id).where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityEvidenceMembership.id.in_(closure.membership_ids),
            ).order_by(EntityEvidenceMembership.id).with_for_update()
        )
    if closure.alias_ids:
        await session.scalars(
            select(EntityAlias.id).where(
                EntityAlias.entity_id.in_(workspace_entities), EntityAlias.id.in_(closure.alias_ids),
            ).order_by(EntityAlias.id).with_for_update()
        )
    if closure.alias_evidence_ids:
        await session.scalars(
            select(EntityAliasEvidence.id).where(
                EntityAliasEvidence.membership_id.in_(workspace_memberships),
                EntityAliasEvidence.id.in_(closure.alias_evidence_ids),
            ).order_by(EntityAliasEvidence.id).with_for_update()
        )
    if closure.field_evidence_ids:
        await session.scalars(
            select(EntityFieldEvidence.id).where(
                EntityFieldEvidence.membership_id.in_(workspace_memberships),
                EntityFieldEvidence.id.in_(closure.field_evidence_ids),
            ).order_by(EntityFieldEvidence.id).with_for_update()
        )


async def lock_entity_ids(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Lock a bounded sorted set of entity rows for support cleanup."""
    ids = sorted(set(entity_ids), key=str)
    if len(ids) > 10_000:
        raise ValueError("Entity support cleanup exceeds its atomic limit")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if ids:
        await session.scalars(
            select(Entity.id).where(
                Entity.workspace_id == scope.workspace_id, Entity.id.in_(ids),
            ).order_by(Entity.id).with_for_update()
        )


async def remove_document_support(
    session: AsyncSession, closure: EntitySupportClosure, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> int:
    """Remove prepared evidence memberships and unsupported derived fields for one document."""
    if closure.document_id is None:
        raise ValueError("A document closure is required")
    return await _remove_entity_support(
        session, closure, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def remove_source_support(
    session: AsyncSession, closure: EntitySupportClosure, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> int:
    """Remove prepared evidence memberships and unsupported derived fields for one Source."""
    if closure.document_id is not None:
        raise ValueError("A Source-wide closure is required")
    return await _remove_entity_support(
        session, closure, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def _remove_entity_support(
    session: AsyncSession, closure: EntitySupportClosure, *, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> int:
    """Delete prepared evidence and aliases, preserving owner aliases and supported values.

    Rows were locked by ``prepare_support_cleanup_in_uow``; this never locks.
    """
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        source_id=closure.source_id, scope=scope, access_fence=access_fence, source_fence=source_fence,
    )
    current = await support_cleanup_ids(
        session, source_id=closure.source_id, document_id=closure.document_id,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current.overflow or current != closure:
        raise RuntimeError("cleanup closure changed")
    workspace_id = scope.workspace_id
    source_wide = closure.document_id is None
    if closure.alias_evidence_ids:
        await session.execute(
            delete(EntityAliasEvidence).where(
                EntityAliasEvidence.id.in_(closure.alias_evidence_ids),
                EntityAliasEvidence.membership_id.in_(
                    select(EntityEvidenceMembership.id).where(EntityEvidenceMembership.workspace_id == workspace_id)
                ),
            )
        )
    if closure.membership_ids:
        await session.execute(
            delete(EntityEvidenceMembership).where(
                EntityEvidenceMembership.workspace_id == workspace_id,
                EntityEvidenceMembership.id.in_(closure.membership_ids),
            )
        )
    for alias_id in closure.alias_ids:
        alias = await session.get(EntityAlias, alias_id)
        if alias is None:
            continue
        if source_wide and alias.source_id == closure.source_id:
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
                EntityEvidenceMembership.workspace_id == workspace_id,
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
    await _clear_unsupported_derived_fields(session, set(closure.entity_ids), workspace_id=workspace_id)
    return len(closure.membership_ids)


async def _clear_unsupported_derived_fields(
    session: AsyncSession, entity_ids: set[UUID], *, workspace_id: UUID,
) -> None:
    """Clear non-owner entity fields whose exact current evidence support was removed."""
    for entity_id in sorted(entity_ids):
        entity = await session.get(Entity, entity_id)
        if entity is not None and entity.workspace_id == workspace_id:
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
                        EntityEvidenceMembership.workspace_id == workspace_id,
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
                        EntityEvidenceMembership.workspace_id == workspace_id,
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
    *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, str]]]:
    """Read-only cursor page of entities by ``(updated_at, id)`` for the automations sweep.

    The key combines id and ``updated_at`` so every change is a distinct trigger event. Payload
    carries id, type and a created/updated marker only.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    stmt = select(Entity).where(Entity.workspace_id == scope.workspace_id)
    if position is not None:
        stmt = stmt.where(tuple_(Entity.updated_at, Entity.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(Entity.updated_at, Entity.id).limit(limit))).all()
    return [(r.updated_at, r.id, f"{r.id}:{r.updated_at.isoformat()}",
             {"entity_id": str(r.id), "entity_type": r.type,
              "change": "created" if r.revision == 1 else "updated"}) for r in rows]

