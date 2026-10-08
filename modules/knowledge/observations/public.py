"""Owner-scoped observation revision writes, selection and bounded reads."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Table, and_, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import (
    GeospatialObservationPage,
    GeospatialObservationRead,
    ObservationExportFence,
    ObservationExportFenceValidation,
    ObservationExportPage,
    ObservationExportRead,
    ObservationQuery,
    WorldMeasurement,
)
from modules.sources import public as sources
from modules.sources.schemas import SourceExportFence, SourceFence

OBSERVATION_EXPORT_PAGE_MAX_BYTES = 16_777_216


def _actor(scope: Scope) -> int:
    """Return the actor bound to a validated workspace or internal-job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, lock: bool = False,
) -> AccessFence:
    """Admit owner-only observation access before domain reads or writes; W3 owns member grants."""
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _observation_export_cursor(
    owner_id: int, workspace_id: UUID, snapshot_at: datetime, accepted_at: datetime, row_id: UUID,
    access_fence: AccessFence,
) -> str:
    """Encode owner/workspace, admission revisions, cutoff and accepted-time/ID position."""
    if owner_id != access_fence.user_id or workspace_id != access_fence.workspace_id:
        raise ValueError("Observation export cursor identity does not match its access fence")
    value = {"v": 3, "owner": owner_id, "workspace": str(workspace_id), "kind": "observations",
             "membership_revision": access_fence.membership_revision,
             "configuration_revision": access_fence.configuration_revision,
             "snapshot": snapshot_at.astimezone(UTC).isoformat(),
             "at": accepted_at.astimezone(UTC).isoformat(), "id": str(row_id)}
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_observation_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID, access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Validate owner, workspace and admission revisions for the canonical export keyset."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {
            "v", "owner", "workspace", "kind", "membership_revision", "configuration_revision",
            "snapshot", "at", "id",
        }:
            raise ValueError
        if (value["v"] != 3 or value["owner"] != owner_id
                or value["workspace"] != str(workspace_id) or value["kind"] != "observations"
                or owner_id != access_fence.user_id or workspace_id != access_fence.workspace_id
                or value["membership_revision"] != access_fence.membership_revision
                or value["configuration_revision"] != access_fence.configuration_revision):
            raise ValueError
        snapshot, position = datetime.fromisoformat(value["snapshot"]), datetime.fromisoformat(value["at"])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot, position)):
            raise ValueError
        snapshot, position = snapshot.astimezone(UTC), position.astimezone(UTC)
        row_id = UUID(value["id"])
        if (snapshot > datetime.now(UTC)
                or _observation_export_cursor(
                    owner_id, workspace_id, snapshot, position, row_id, access_fence,
                ) != cursor):
            raise ValueError
        return snapshot, position, row_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid observation export cursor") from exc


def _observation_export_digest(item: ObservationExportRead) -> str:
    """Hash the exact allowlisted portable record rather than internal ORM state."""
    return hashlib.sha256(json.dumps(item.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def _observation_export_count(
    session: AsyncSession, snapshot_at: datetime, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Count only rows whose current source scope and exact document version remain valid."""
    # ponytail: exact public evidence filtering scans O(N) per page; replace with a Documents-owned
    # SQL eligibility count only when capacity evidence shows this bounded-memory path is too slow.
    base = select(Observation).where(
        Observation.workspace_id == scope.workspace_id,
        Observation.is_current.is_(True), Observation.accepted_at <= snapshot_at,
        Observation.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )
    count, position = 0, None
    while True:
        statement = base
        if position is not None:
            statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
        rows = list((await session.scalars(statement.order_by(Observation.accepted_at, Observation.id)
                                           .limit(256).execution_options(populate_existing=True))).all())
        if not rows:
            return count
        scope_by_source: dict[UUID, Any] = {}
        candidates = []
        for row in rows:
            provider_scope = scope_by_source.get(row.source_id)
            if row.source_id not in scope_by_source:
                source = await sources.get_connector_source(
                    session, row.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                provider_scope = (await connectors.export_provider_scope(
                    session, row.source_id, source.generation, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                         if source is not None else None)
                scope_by_source[row.source_id] = provider_scope
            provider_scope = scope_by_source[row.source_id]
            if provider_scope is not None and provider_scope.provider_id == row.provider:
                candidates.append((documents.ObservationExportEvidenceCandidate(
                    observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
                    provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
                    external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
                ), provider_scope))
        for candidate, provider_scope in candidates:
            count += int(await documents.export_observation_evidence(
                session, candidate, provider_scope, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            ) is not None)
        position = (rows[-1].accepted_at, rows[-1].id)


async def export_page(session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50,
                      cursor: str | None = None, scope: Scope, multi_workspace_enabled: bool) -> ObservationExportPage:
    """Export bounded actor/workspace measurements with current Source and exact retained Documents evidence."""
    if record_kind != "observations" or not 1 <= limit <= 100:
        raise ValueError("Observation export kind or limit is invalid")
    if owner_id != _actor(scope):
        raise ValueError("Observation export actor does not match the admitted scope")
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if cursor is None:
        snapshot, position = datetime.now(UTC), None
    else:
        snapshot, position_at, position_id = _decode_observation_export_cursor(
            cursor, owner_id, scope.workspace_id, access_fence,
        )
        position = (position_at, position_id)
    count = await _observation_export_count(
        session, snapshot, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    statement = select(Observation).where(
        Observation.workspace_id == scope.workspace_id,
        Observation.is_current.is_(True), Observation.accepted_at <= snapshot,
        Observation.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )
    if position is not None:
        statement = statement.where(tuple_(Observation.accepted_at, Observation.id) > position)
    rows = list((await session.scalars(statement.order_by(Observation.accepted_at, Observation.id)
                                       .limit(limit + 1).execution_options(populate_existing=True))).all())
    more = len(rows) > limit
    items: list[ObservationExportRead] = []
    fences: list[ObservationExportFence] = []
    last_examined: tuple[datetime, UUID] | None = None
    for row in rows[:limit]:
        source = await sources.get_connector_source(
            session, row.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if source is None:
            last_examined = (row.accepted_at, row.id)
            continue
        provider_scope = await connectors.export_provider_scope(
            session, row.source_id, source.generation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if provider_scope is None or provider_scope.provider_id != row.provider:
            last_examined = (row.accepted_at, row.id)
            continue
        evidence = await documents.export_observation_evidence(session, documents.ObservationExportEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
        ), provider_scope, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if evidence is None:
            last_examined = (row.accepted_at, row.id)
            continue
        item = ObservationExportRead(
            id=row.id, source_id=row.source_id, source_generation=row.source_generation,
            provider=row.provider, external_id=row.external_id, revision=row.revision, metric=row.metric,
            symbol=row.symbol, region=row.region, latitude=row.latitude, longitude=row.longitude,
            observed_at=row.observed_at, published_at=row.published_at, collected_at=row.collected_at,
            accepted_at=row.accepted_at, value=row.value, unit=row.unit, currency=row.currency,
            timezone=row.timezone, quality=row.quality, missing_reason=row.missing_reason,
            document_id=row.document_id, document_version_id=row.document_version_id,
        )
        proposed = items + [item]
        size = len(json.dumps([value.model_dump(mode="json") for value in proposed], ensure_ascii=False,
                              separators=(",", ":")).encode())
        if size > OBSERVATION_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An observation export record exceeds the page byte budget")
            more = True
            break
        items.append(item)
        last_examined = (row.accepted_at, row.id)
        fences.append(ObservationExportFence(
            id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            current_source_generation=evidence.current_source_generation,
            provider_scope_digest=hashlib.sha256(provider_scope.discriminator.encode()).hexdigest(),
            record_digest=_observation_export_digest(item),
        ))
    next_cursor = (_observation_export_cursor(
        owner_id, scope.workspace_id, snapshot, last_examined[0], last_examined[1], access_fence,
    )
                   if more and last_examined is not None else None)
    payload = len(json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False,
                             separators=(",", ":")).encode())
    return ObservationExportPage(owner_id=owner_id, snapshot_at=snapshot, snapshot_count=count,
                                 items=items, fences=fences, payload_bytes=payload, next_cursor=next_cursor)


async def validate_export_fences(session: AsyncSession, *, owner_id: int, record_kind: str,
                                 snapshot_at: datetime, expected_snapshot_count: int,
                                 fences: list[ObservationExportFence], scope: Scope,
                                 multi_workspace_enabled: bool) -> ObservationExportFenceValidation:
    """Recheck one admitted actor/workspace snapshot, provider scope, generation and exact exported fields."""
    if record_kind != "observations" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Observation export validation input is invalid")
    if owner_id != _actor(scope):
        raise ValueError("Observation export actor does not match the admitted scope")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    observed = await _observation_export_count(
        session, snapshot_at, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if observed != expected_snapshot_count:
        return ObservationExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Observation).where(
            Observation.id == fence.id, Observation.workspace_id == scope.workspace_id,
        ).execution_options(populate_existing=True))
        if (row is None or row.source_id != fence.source_id
                or row.source_generation != fence.accepted_source_generation):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        source = await sources.get_connector_source(
            session, row.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        provider_scope = (await connectors.export_provider_scope(
            session, row.source_id, source.generation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
                 if source is not None else None)
        if (source is None or source.generation != fence.current_source_generation or provider_scope is None
                or provider_scope.provider_id != row.provider
                or hashlib.sha256(provider_scope.discriminator.encode()).hexdigest() != fence.provider_scope_digest):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = ObservationExportRead(
            id=row.id, source_id=row.source_id, source_generation=row.source_generation, provider=row.provider,
            external_id=row.external_id, revision=row.revision, metric=row.metric, symbol=row.symbol, region=row.region,
            latitude=row.latitude, longitude=row.longitude, observed_at=row.observed_at, published_at=row.published_at,
            collected_at=row.collected_at, accepted_at=row.accepted_at, value=row.value, unit=row.unit,
            currency=row.currency, timezone=row.timezone, quality=row.quality, missing_reason=row.missing_reason,
            document_id=row.document_id, document_version_id=row.document_version_id,
        )
        evidence = await documents.export_observation_evidence(session, documents.ObservationExportEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, accepted_source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id, document_version_id=row.document_version_id,
        ), provider_scope, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        eligible = await sources.filter_export_eligible_sources(session, [SourceExportFence(
            source_id=fence.source_id, workspace_id=scope.workspace_id,
            generation=fence.current_source_generation,
        )], scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if (not row.is_current or row.accepted_at > snapshot_at or row.source_id not in eligible
                or evidence is None or evidence.current_source_generation != fence.current_source_generation):
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        if _observation_export_digest(item) != fence.record_digest:
            return ObservationExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return ObservationExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)


@dataclass(frozen=True)
class ObservationWriteResult:
    """Expose only the accepted row identity and whether current series output changed."""
    observation_id: UUID
    selected_current: bool
    current_changed: bool


@dataclass(frozen=True)
class IngestionObservationAcceptedState:
    """Exact accepted-pair identity/absence, never a portable permission or lock token."""
    external_id: str
    ingestion_observation_id: UUID
    observation_id: UUID | None

    def __post_init__(self) -> None:
        """Require exact key spelling and UUID identities before owner discovery."""
        _validate_observation_key(self.external_id)
        if (not isinstance(self.ingestion_observation_id, UUID)
                or (self.observation_id is not None and not isinstance(self.observation_id, UUID))):
            raise ValueError("Observation accepted state requires exact UUIDs")


@dataclass(frozen=True)
class IngestionObservationSeriesState:
    """Store current identity and revision maximum once per repeated external key."""
    external_id: str
    current_observation_id: UUID | None
    max_revision: int

    def __post_init__(self) -> None:
        """Reject malformed/nonpositive current-series identities and negative maxima."""
        _validate_observation_key(self.external_id)
        if (type(self.max_revision) is not int or self.max_revision < 0
                or (self.current_observation_id is not None and (
                    not isinstance(self.current_observation_id, UUID) or self.max_revision == 0))):
            raise ValueError("Observation series state is invalid")


@dataclass(frozen=True)
class IngestionObservationPreparation:
    """Bounded immutable accepted pairs plus one shared current/max state per key.

    Actual admission/Source/Document/Observation locks stay in the transaction;
    snapshots cannot authorize calls, add keys, survive rollback or create lineage.
    """
    workspace_id: UUID
    source_id: UUID
    source_generation: int
    accepted: tuple[IngestionObservationAcceptedState, ...]
    series: tuple[IngestionObservationSeriesState, ...]

    def __post_init__(self) -> None:
        """Require <=32 unique pairs and precisely their unique shared series keys."""
        if (not isinstance(self.workspace_id, UUID) or not isinstance(self.source_id, UUID)
                or type(self.source_generation) is not int or self.source_generation <= 0
                or type(self.accepted) is not tuple or type(self.series) is not tuple
                or len(self.accepted) > 32 or len(self.series) > 32
                or any(not isinstance(item, IngestionObservationAcceptedState) for item in self.accepted)
                or any(not isinstance(item, IngestionObservationSeriesState) for item in self.series)):
            raise ValueError("Observation preparation header or bound is invalid")
        pairs = {(item.external_id, item.ingestion_observation_id) for item in self.accepted}
        keys = {item.external_id for item in self.series}
        if (len(pairs) != len(self.accepted) or len(keys) != len(self.series)
                or keys != {item.external_id for item in self.accepted}):
            raise ValueError("Observation preparation pair/series membership is invalid")


def _validate_observation_key(external_id: str) -> None:
    """Validate a stored external key without stripping, normalizing or truncating it."""
    if type(external_id) is not str or not 1 <= len(external_id) <= 512:
        raise ValueError("Observation external key requires1..512 exact characters")


@dataclass(frozen=True)
class ObservationScopeRead:
    """Pair each selected point with the public current source and provider scope fence."""
    source_id: UUID
    source_generation: int
    provider: str
    discriminator: str


async def _observation_source_proof(
    session: AsyncSession, source_id: UUID, source_generation: int, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Compare full original admission/Source proof nonlockingly under retained locks.

    Current owner/default-workspace and source-bound scope are checked by public
    owners. Access disagreement or malformed/foreign original proof is a whole
    transaction conflict. Unavailable/inactive Source returns False before any
    mutation; callers cannot substitute a newer generation or membership.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise RuntimeError("observation_preparation_fence_required")
    if (source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or access_fence.workspace_id != scope.workspace_id or source_fence.generation != source_generation
            or source_fence.status != "active"):
        raise RuntimeError("observation_preparation_header_changed")
    current_access = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if current_access != access_fence:
        raise RuntimeError("observation_preparation_access_changed")
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_source == source_fence


async def _observation_key_snapshot(
    session: AsyncSession, source_id: UUID, keys: tuple[tuple[str, UUID], ...], workspace_id: UUID,
) -> tuple[tuple[IngestionObservationAcceptedState, ...], tuple[IngestionObservationSeriesState, ...],
           dict[tuple[str, UUID], Observation], dict[str, Observation]]:
    """Freshly read accepted/current union and per-key maxima, without locks or writes.

    Source serializes every relevant revision/current writer. Duplicate currents,
    foreign namespace, duplicate accepted pairs and impossible revisions abort;
    no LIMIT1 silently picks an incumbent. Bounded <=32 requested pairs/keys
    produce <=64 union rows; aggregate maxima remain inside the database.
    """
    external_ids = tuple(dict.fromkeys(key[0] for key in keys))
    rows = list((await session.scalars(select(Observation).where(
        Observation.source_id == source_id,
        or_(tuple_(Observation.external_id, Observation.ingestion_observation_id).in_(keys),
            and_(Observation.external_id.in_(external_ids), Observation.is_current.is_(True))),
    ).limit(65).execution_options(populate_existing=True))).all())
    if len(rows) > 64:
        raise RuntimeError("observation_preparation_identity_set_changed")
    accepted: dict[tuple[str, UUID], Observation] = {}
    current: dict[str, Observation] = {}
    for row in rows:
        if (row.workspace_id != workspace_id or row.source_id != source_id
                or row.external_id not in external_ids or row.revision <= 0):
            raise RuntimeError("observation_preparation_namespace_changed")
        pair = (row.external_id, row.ingestion_observation_id)
        if pair in keys:
            if pair in accepted:
                raise RuntimeError("observation_preparation_duplicate_pair")
            accepted[pair] = row
        if row.is_current:
            if row.external_id in current:
                raise RuntimeError("observation_preparation_multiple_current")
            current[row.external_id] = row
    maxima = (await session.execute(select(
        Observation.external_id, func.max(Observation.revision),
        func.count().filter(Observation.workspace_id != workspace_id),
    ).where(Observation.source_id == source_id, Observation.external_id.in_(external_ids))
      .group_by(Observation.external_id))).all()
    if any(foreign_count or maximum is None or maximum <= 0 for _, maximum, foreign_count in maxima):
        raise RuntimeError("observation_preparation_series_namespace_changed")
    maximums = {external_id: maximum for external_id, maximum, _ in maxima}
    try:
        accepted_state = tuple(IngestionObservationAcceptedState(
            external_id=external_id, ingestion_observation_id=accepted_id,
            observation_id=accepted[(external_id, accepted_id)].id if (external_id, accepted_id) in accepted else None,
        ) for external_id, accepted_id in keys)
        series_state = tuple(IngestionObservationSeriesState(
            external_id=external_id, current_observation_id=current[external_id].id if external_id in current else None,
            max_revision=int(maximums.get(external_id) or 0),
        ) for external_id in external_ids)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("observation_preparation_stored_shape_changed") from exc
    if any(row.revision > int(maximums.get(row.external_id) or 0) for row in rows):
        raise RuntimeError("observation_preparation_revision_changed")
    return accepted_state, series_state, accepted, current


async def prepare_ingestion_observation_keys(
    session: AsyncSession, source_id: UUID, keys: tuple[tuple[str, UUID], ...], *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> IngestionObservationPreparation:
    """Lock the deduplicated accepted/current union once in ascending Observation UUID.

    Caller retains admission/Source and earlier D/identity plus finite Ingestion
    roots/journals; EventOutbox/replay remain unlocked. Reject input over32 before
    dedup/query. Fresh post-lock set/current/max comparisons must match discovery;
    changed ownership aborts without another earlier acquisition. Empty allowed;
    no placeholder/current mutation, session registry, commit or provider I/O.
    """
    if not isinstance(source_id, UUID) or type(keys) is not tuple or len(keys) > 32:
        raise ValueError("Observation preparation requires at most32 exact pairs")
    for key in keys:
        if type(key) is not tuple or len(key) != 2 or not isinstance(key[1], UUID):
            raise ValueError("Observation preparation pair is invalid")
        _validate_observation_key(key[0])
    keys = tuple(dict.fromkeys(keys))
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("observation_preparation_fence_required")
    if not await _observation_source_proof(
        session, source_id, source_fence.generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    ):
        raise RuntimeError("observation_preparation_source_changed")
    before_accepted, before_series, accepted, current = await _observation_key_snapshot(
        session, source_id, keys, scope.workspace_id,
    )
    existing_ids = {row.id for row in (*accepted.values(), *current.values())}
    if existing_ids:
        await session.scalars(select(Observation).where(Observation.id.in_(existing_ids))
                              .order_by(Observation.id).with_for_update().execution_options(populate_existing=True))
    after_accepted, after_series, _, _ = await _observation_key_snapshot(session, source_id, keys, scope.workspace_id)
    if before_accepted != after_accepted or before_series != after_series:
        raise RuntimeError("observation_preparation_identity_changed")
    return IngestionObservationPreparation(workspace_id=scope.workspace_id, source_id=source_id,
        source_generation=source_fence.generation, accepted=after_accepted, series=after_series)


async def upsert_from_ingestion(
    session: AsyncSession, *, source_id: UUID, source_generation: int,
    ingestion_observation_id: UUID, document_id: UUID, document_version_id: UUID,
    external_id: str, provider_version: str | None, observed_at: datetime,
    collected_at: datetime, accepted_at: datetime, provider_scope_discriminator: str,
    measurement: WorldMeasurement, scope: Scope, multi_workspace_enabled: bool,
) -> ObservationWriteResult | None:
    """Acquire Source, D parents and Observation union, preserving ordinary optional result.

    Entry has no later domain/outbox locks. The wrapper captures current admission
    for this immediate transaction; deferred normalization uses original fences
    and explicit preparation. Invalid Source/provider returns None before writes;
    exact duplicate/current ordering and owner result fields remain unchanged.
    """
    source_fence = await sources.lock_source(session, source_id, scope=scope,
                                             multi_workspace_enabled=multi_workspace_enabled)
    if (source_fence is None or source_fence.status != "active" or source_fence.generation != source_generation):
        return None
    access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await documents.prepare_normalized_document_keys(session, source_id, (external_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    preparation = await prepare_ingestion_observation_keys(
        session, source_id, ((external_id, ingestion_observation_id),), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    result, _ = await upsert_from_ingestion_in_uow(
        session, source_id=source_id, source_generation=source_generation,
        ingestion_observation_id=ingestion_observation_id, document_id=document_id, document_version_id=document_version_id,
        external_id=external_id, provider_version=provider_version, observed_at=observed_at, collected_at=collected_at,
        accepted_at=accepted_at, provider_scope_discriminator=provider_scope_discriminator, measurement=measurement,
        preparation=preparation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    return result


async def upsert_from_ingestion_in_uow(
    session: AsyncSession, *, source_id: UUID, source_generation: int,
    ingestion_observation_id: UUID, document_id: UUID, document_version_id: UUID,
    external_id: str, provider_version: str | None, observed_at: datetime,
    collected_at: datetime, accepted_at: datetime, provider_scope_discriminator: str,
    measurement: WorldMeasurement, preparation: IngestionObservationPreparation,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> tuple[ObservationWriteResult | None, IngestionObservationPreparation]:
    """Apply one prepared pair and advance only its accepted ID and shared series.

    Retain actual earlier admission/Source/D/identity/Ingestion/Observation locks;
    freshly compare original full fences, exact accepted absence/current/max and
    D-owned exact version provenance. Ingestion supplies its already-locked fresh
    immutable acceptance lineage and prepared FK parents, never a generated ID.
    No earlier lock or ON CONFLICT adoption follows outbox. New local IDs/max+1
    are verified before the immutable successor; all mismatches abort. A rejected
    Source/provider returns (None, unchanged) only before O mutation, and caller
    must roll back prior D work as a whole attempt. No commit or external I/O.
    """
    if (not isinstance(preparation, IngestionObservationPreparation)
            or preparation.workspace_id != scope.workspace_id or preparation.source_id != source_id
            or preparation.source_generation != source_generation):
        raise RuntimeError("observation_preparation_header_changed")
    pairs = tuple((item.external_id, item.ingestion_observation_id) for item in preparation.accepted)
    pair = (external_id, ingestion_observation_id)
    expected_accepted = next((item for item in preparation.accepted
                              if (item.external_id, item.ingestion_observation_id) == pair), None)
    expected_series = next((item for item in preparation.series if item.external_id == external_id), None)
    if expected_accepted is None or expected_series is None:
        raise RuntimeError("observation_preparation_key_missing")
    if not await _observation_source_proof(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    ):
        return None, preparation
    normalized = WorldMeasurement.model_validate(measurement.model_dump(mode="python"))
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        return None, preparation
    if any(value.tzinfo is None or value.utcoffset() is None for value in (observed_at, collected_at)):
        raise ValueError("Observation clocks must be timezone aware")
    provider_scope = await connectors.get_current_provider_scope(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (provider_scope is None or provider_scope.provider_id != normalized.provider
            or provider_scope.workspace_id != scope.workspace_id or provider_scope.source_id != source_id
            or provider_scope.source_generation != source_generation
            or provider_scope.discriminator != provider_scope_discriminator):
        return None, preparation
    fresh_accepted, fresh_series, accepted, current = await _observation_key_snapshot(
        session, source_id, pairs, scope.workspace_id,
    )
    if fresh_accepted != preparation.accepted or fresh_series != preparation.series:
        raise RuntimeError("observation_preparation_identity_changed")
    if not await documents.normalized_observation_version_matches_in_uow(
        session, source_id=source_id, source_generation=source_generation, document_id=document_id,
        document_version_id=document_version_id, external_id=external_id, provider=normalized.provider,
        provider_scope_discriminator=provider_scope_discriminator, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    ):
        raise RuntimeError("observation_preparation_document_provenance_changed")
    digest = hashlib.sha256(json.dumps(
        {**normalized.model_dump(mode="json"), "observed_at": observed_at.astimezone(UTC).isoformat()},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    existing = accepted.get(pair)
    if existing is not None:
        if (existing.source_generation != source_generation or existing.provider != normalized.provider
                or existing.provider_scope_discriminator != provider_scope_discriminator):
            raise RuntimeError("observation_preparation_accepted_lineage_changed")
        if existing.content_hash != digest:
            raise ValueError("ingestion observation was reused for different provider content")
        return ObservationWriteResult(existing.id, existing.is_current, False), preparation
    result, row = await _apply_ingestion_observation(
        session, source_id=source_id, source_generation=source_generation, workspace_id=scope.workspace_id,
        ingestion_observation_id=ingestion_observation_id, document_id=document_id, document_version_id=document_version_id,
        external_id=external_id, provider_version=provider_version, observed_at=observed_at, collected_at=collected_at,
        accepted_at=accepted_at, provider_scope_discriminator=provider_scope_discriminator, normalized=normalized,
        digest=digest, current=current.get(external_id), highest_revision=expected_series.max_revision,
    )
    successor = IngestionObservationPreparation(
        workspace_id=preparation.workspace_id, source_id=source_id, source_generation=source_generation,
        accepted=tuple(IngestionObservationAcceptedState(external_id, ingestion_observation_id, row.id)
                       if (item.external_id, item.ingestion_observation_id) == pair else item
                       for item in preparation.accepted),
        series=tuple(IngestionObservationSeriesState(external_id,
            row.id if result.selected_current else item.current_observation_id, row.revision)
            if item.external_id == external_id else item for item in preparation.series),
    )
    after_accepted, after_series, _, _ = await _observation_key_snapshot(session, source_id, pairs, scope.workspace_id)
    if after_accepted != successor.accepted or after_series != successor.series:
        raise RuntimeError("observation_preparation_local_write_changed")
    return result, successor


async def _apply_ingestion_observation(
    session: AsyncSession, *, source_id: UUID, source_generation: int, workspace_id: UUID,
    ingestion_observation_id: UUID, document_id: UUID, document_version_id: UUID,
    external_id: str, provider_version: str | None, observed_at: datetime, collected_at: datetime,
    accepted_at: datetime, provider_scope_discriminator: str, normalized: WorldMeasurement,
    digest: str, current: Observation | None, highest_revision: int,
) -> tuple[ObservationWriteResult, Observation]:
    """Insert one local immutable revision and change only the prepared current root.

    All validation/provenance/absence checks precede this body. Source protects
    max+1. Current uses (accepted_at UTC, str(accepted ingestion ID)); a losing
    new revision still advances max. Retain the local UUID before flush and
    compare exact inserted state, without fetching/adopting a unique-key winner.
    Database errors roll back the caller's complete D/O attempt; never commit.
    """
    accepted_utc = accepted_at.astimezone(UTC)
    is_current = current is None or (accepted_utc, str(ingestion_observation_id)) > (
        current.accepted_at.astimezone(UTC), str(current.ingestion_observation_id),
    )
    row_id = uuid4()
    row = Observation(
        id=row_id, workspace_id=workspace_id, source_id=source_id, external_id=external_id, provider=normalized.provider,
        provider_scope_discriminator=provider_scope_discriminator, provider_version=provider_version,
        revision=highest_revision + 1, metric=normalized.metric, symbol=normalized.symbol, region=normalized.region,
        latitude=normalized.latitude, longitude=normalized.longitude, observed_at=observed_at,
        published_at=normalized.published_at, collected_at=collected_at, accepted_at=accepted_utc,
        value=normalized.value, unit=normalized.unit, currency=normalized.currency, quality=normalized.quality,
        missing_reason=normalized.missing_reason, timezone=normalized.timezone, provider_delay_seconds=None,
        is_current=is_current, content_hash=digest, document_id=document_id, document_version_id=document_version_id,
        ingestion_observation_id=ingestion_observation_id, source_generation=source_generation,
    )
    if current is not None and is_current:
        current.is_current = False
    session.add(row)
    await session.flush()
    if (row.id != row_id or row.revision != highest_revision + 1 or row.workspace_id != workspace_id
            or row.source_id != source_id or row.external_id != external_id or row.document_id != document_id
            or row.document_version_id != document_version_id or row.ingestion_observation_id != ingestion_observation_id
            or row.source_generation != source_generation or row.content_hash != digest or row.is_current != is_current):
        raise RuntimeError("observation_preparation_insert_changed")
    return ObservationWriteResult(row.id, is_current, is_current), row

def _cursor_fingerprint(
    query: ObservationQuery, scopes: tuple[ObservationScopeRead, ...], *, access_fence: AccessFence,
) -> str:
    """Bind continuation cursors to filters, source snapshots and admission revisions."""
    payload = {
        "workspace": str(access_fence.workspace_id),
        "actor": access_fence.user_id,
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "filters": query.model_dump(mode="json", exclude={"limit"}),
        "scopes": [
            [str(item.source_id), item.source_generation, item.provider, item.discriminator]
            for item in scopes
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Decode one bounded cursor and reject malformed or cross-filter reuse."""
    if len(cursor) > 2048:
        raise HTTPException(status_code=422, detail="Observation cursor is invalid")
    try:
        raw = base64.b64decode(cursor.encode() + b"=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        identity, stamp, row_id = json.loads(raw)
        if identity != fingerprint:
            raise ValueError
        parsed = datetime.fromisoformat(stamp)
        parsed_id = UUID(row_id)
        if parsed.tzinfo is None or parsed.utcoffset() is None or str(parsed_id) != row_id:
            raise ValueError
        return parsed.astimezone(UTC), parsed_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Observation cursor is invalid") from exc


async def list_observations(
    session: AsyncSession, query: ObservationQuery, cursor: str | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[tuple[Observation, ...], str | None, bool, dict[UUID, int]]:
    """Read bounded current-evidence points after owner admission and workspace/source filters."""
    access_fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    scopes: list[ObservationScopeRead] = []
    current_scopes: dict[UUID, object] = {}
    for source_id in sorted(query.source_ids, key=str):
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        snapshot = await connectors.get_current_provider_scope(
            session, source_id,
            source.generation if source is not None and source.status == "active" else -1,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if snapshot is None:
            raise HTTPException(status_code=404, detail="One or more sources are unavailable")
        current_scopes[source_id] = snapshot
        scopes.append(ObservationScopeRead(
            source_id=snapshot.source_id, source_generation=snapshot.source_generation,
            provider=snapshot.provider_id, discriminator=snapshot.discriminator,
        ))
    source_scope_filters = tuple(and_(
        Observation.source_id == item.source_id,
        Observation.source_generation == item.source_generation,
        Observation.provider == item.provider,
        Observation.provider_scope_discriminator == item.discriminator,
    ) for item in scopes)
    statement = select(Observation).where(
        Observation.workspace_id == scope.workspace_id,
        or_(*source_scope_filters), Observation.is_current.is_(True),
        Observation.observed_at >= query.from_at, Observation.observed_at < query.to_at,
    )
    if query.geospatial_only:
        # Do not infer point coordinates from a location label; only provider-recorded points map.
        statement = statement.where(
            Observation.latitude.is_not(None), Observation.longitude.is_not(None),
        )
    if query.metrics:
        statement = statement.where(Observation.metric.in_(query.metrics))
    if query.symbols:
        statement = statement.where(Observation.symbol.in_(query.symbols))
    if query.regions:
        statement = statement.where(Observation.region.in_(query.regions))
    scopes_tuple = tuple(scopes)
    fingerprint = _cursor_fingerprint(query, scopes_tuple, access_fence=access_fence)
    if cursor:
        stamp, row_id = _decode_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Observation.observed_at, Observation.id) < tuple_(stamp, row_id))
    page: list[Observation] = []
    scanned = 0
    last_scanned: Observation | None = None
    page_version_numbers: dict[UUID, int] = {}
    scan_truncated = False
    while scanned < 2_048 and len(page) <= query.limit:
        batch = tuple((await session.scalars(
            statement.order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(min(256, 2_048 - scanned))
        )).all())
        if not batch:
            break
        candidates = tuple(documents.ObservationEvidenceCandidate(
            observation_id=row.id, source_id=row.source_id, source_generation=row.source_generation,
            provider=row.provider, provider_scope_discriminator=row.provider_scope_discriminator,
            external_id=row.external_id, document_id=row.document_id,
            document_version_id=row.document_version_id,
        ) for row in batch)
        allowed = await documents.current_observation_evidence_versions(
            session, candidates, current_scopes, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        for row in batch:
            if row.id in allowed:
                page.append(row)
                page_version_numbers[row.id] = allowed[row.id]
                if len(page) > query.limit:
                    break
        scanned += len(batch)
        last_scanned = batch[-1]
        statement = statement.where(tuple_(Observation.observed_at, Observation.id) < tuple_(last_scanned.observed_at, last_scanned.id))
        if len(batch) < min(256, 2_048 - (scanned - len(batch))):
            break
        if scanned >= 2_048:
            scan_truncated = True
    has_more = len(page) > query.limit
    page = page[:query.limit]
    page_version_numbers = {row.id: page_version_numbers[row.id] for row in page}
    next_cursor = None
    if has_more and page:
        last = page[-1]
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    elif scan_truncated and last_scanned is not None:
        last = last_scanned
        payload = json.dumps([fingerprint, last.observed_at.astimezone(UTC).isoformat(), str(last.id)], separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return tuple(page), next_cursor, scan_truncated, page_version_numbers


async def list_geospatial_observations(
    session: AsyncSession, query: ObservationQuery, cursor: str | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> GeospatialObservationPage:
    """Return one bounded Open-Meteo page after owner admission and scoped source checks.

    Non-weather and inactive sources are counted as omitted rather than causing a
    broad query failure. The underlying query still applies the standard source,
    provider-scope, generation, current revision and exact-document evidence fences.
    """
    if not query.source_ids:
        raise ValueError("At least one map source is required")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    eligible: list[UUID] = []
    omitted = 0
    for source_id in query.source_ids:
        source = await sources.get_connector_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        snapshot = await connectors.get_current_provider_scope(
            session, source_id,
            source.generation if source is not None and source.status == "active" else -1,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        # Only the existing weather adapter declares point coordinates; market symbols have none.
        if snapshot is None or snapshot.provider_id != "open_meteo":
            omitted += 1
        else:
            eligible.append(source_id)
    if not eligible:
        return GeospatialObservationPage(
            items=[], next_cursor=None, truncated=False, omitted_source_count=omitted,
        )
    scoped_query = query.model_copy(update={
        "source_ids": eligible, "geospatial_only": True,
    })
    rows, next_cursor, truncated, version_numbers = await list_observations(
        session, scoped_query, cursor, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    version_numbers_by_observation = version_numbers
    items = [GeospatialObservationRead.model_validate({
        **{key: getattr(row, key) for key in GeospatialObservationRead.model_fields if hasattr(row, key)},
        "document_version_number": version_numbers_by_observation.get(row.id),
    }) for row in rows]
    return GeospatialObservationPage(
        items=items, next_cursor=next_cursor, truncated=truncated,
        omitted_source_count=omitted,
    )


async def purge_source_in_uow(session: AsyncSession, source_id: UUID) -> None:
    """Remove every derived observation revision in the source's existing deletion transaction."""
    await session.execute(update(Observation).where(Observation.source_id == source_id).values(is_current=False))
    await session.execute(
        cast(Table, Observation.__table__).delete().where(Observation.source_id == source_id)
    )


async def purge_document_in_uow(session: AsyncSession, document_id: UUID) -> None:
    """Remove evidence-supported measurement revisions before their document is deleted."""
    await session.execute(
        cast(Table, Observation.__table__).delete().where(Observation.document_id == document_id)
    )
