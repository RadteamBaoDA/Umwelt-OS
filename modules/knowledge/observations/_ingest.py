"""Ingestion-side observation revision writes (key preparation, upsert) behind ``modules.knowledge.observations.public``."""

# isort: split
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    and_,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.knowledge.observations._snapshot_rows import classify_snapshot_rows
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import (
    WorldMeasurement,
)
from modules.sources import public as sources
from modules.sources.public import SourceFence


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
        if not isinstance(self.ingestion_observation_id, UUID) or (
            self.observation_id is not None and not isinstance(self.observation_id, UUID)
        ):
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
        if (
            type(self.max_revision) is not int
            or self.max_revision < 0
            or (
                self.current_observation_id is not None
                and (not isinstance(self.current_observation_id, UUID) or self.max_revision == 0)
            )
        ):
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
        if (
            not isinstance(self.workspace_id, UUID)
            or not isinstance(self.source_id, UUID)
            or type(self.source_generation) is not int
            or self.source_generation <= 0
            or type(self.accepted) is not tuple
            or type(self.series) is not tuple
            or len(self.accepted) > 32
            or len(self.series) > 32
            or any(
                not isinstance(item, IngestionObservationAcceptedState) for item in self.accepted
            )
            or any(not isinstance(item, IngestionObservationSeriesState) for item in self.series)
        ):
            raise ValueError("Observation preparation header or bound is invalid")
        pairs = {(item.external_id, item.ingestion_observation_id) for item in self.accepted}
        keys = {item.external_id for item in self.series}
        if (
            len(pairs) != len(self.accepted)
            or len(keys) != len(self.series)
            or keys != {item.external_id for item in self.accepted}
        ):
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
    session: AsyncSession,
    source_id: UUID,
    source_generation: int,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> bool:
    """Compare full original admission/Source proof nonlockingly under retained locks.

    Current owner/default-workspace and source-bound scope are checked by public
    owners. Access disagreement or malformed/foreign original proof is a whole
    transaction conflict. Unavailable/inactive Source returns False before any
    mutation; callers cannot substitute a newer generation or membership.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise RuntimeError("observation_preparation_fence_required")
    if (
        source_fence.id != source_id
        or source_fence.workspace_id != scope.workspace_id
        or access_fence.workspace_id != scope.workspace_id
        or source_fence.generation != source_generation
        or source_fence.status != "active"
    ):
        raise RuntimeError("observation_preparation_header_changed")
    current_access = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if current_access != access_fence:
        raise RuntimeError("observation_preparation_access_changed")
    current_source = await sources.get_source_fence(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_source == source_fence


async def _observation_key_snapshot(
    session: AsyncSession,
    source_id: UUID,
    keys: tuple[tuple[str, UUID], ...],
    workspace_id: UUID,
) -> tuple[
    tuple[IngestionObservationAcceptedState, ...],
    tuple[IngestionObservationSeriesState, ...],
    dict[tuple[str, UUID], Observation],
    dict[str, Observation],
]:
    """Freshly read accepted/current union and per-key maxima, without locks or writes.

    Source serializes every relevant revision/current writer. Duplicate currents,
    foreign namespace, duplicate accepted pairs and impossible revisions abort;
    no LIMIT1 silently picks an incumbent. Bounded <=32 requested pairs/keys
    produce <=64 union rows; aggregate maxima remain inside the database.
    """
    external_ids = tuple(dict.fromkeys(key[0] for key in keys))
    rows = list(
        (
            await session.scalars(
                select(Observation)
                .where(
                    Observation.source_id == source_id,
                    or_(
                        tuple_(Observation.external_id, Observation.ingestion_observation_id).in_(
                            keys
                        ),
                        and_(
                            Observation.external_id.in_(external_ids),
                            Observation.is_current.is_(True),
                        ),
                    ),
                )
                .limit(65)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(rows) > 64:
        raise RuntimeError("observation_preparation_identity_set_changed")
    accepted, current = classify_snapshot_rows(rows, keys, external_ids, source_id, workspace_id)
    maxima = (
        await session.execute(
            select(
                Observation.external_id,
                func.max(Observation.revision),
                func.count().filter(Observation.workspace_id != workspace_id),
            )
            .where(Observation.source_id == source_id, Observation.external_id.in_(external_ids))
            .group_by(Observation.external_id)
        )
    ).all()
    if any(
        foreign_count or maximum is None or maximum <= 0 for _, maximum, foreign_count in maxima
    ):
        raise RuntimeError("observation_preparation_series_namespace_changed")
    maximums = {external_id: maximum for external_id, maximum, _ in maxima}
    try:
        accepted_state = tuple(
            IngestionObservationAcceptedState(
                external_id=external_id,
                ingestion_observation_id=accepted_id,
                observation_id=accepted[(external_id, accepted_id)].id
                if (external_id, accepted_id) in accepted
                else None,
            )
            for external_id, accepted_id in keys
        )
        series_state = tuple(
            IngestionObservationSeriesState(
                external_id=external_id,
                current_observation_id=current[external_id].id if external_id in current else None,
                max_revision=int(maximums.get(external_id) or 0),
            )
            for external_id in external_ids
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("observation_preparation_stored_shape_changed") from exc
    if any(row.revision > int(maximums.get(row.external_id) or 0) for row in rows):
        raise RuntimeError("observation_preparation_revision_changed")
    return accepted_state, series_state, accepted, current


async def prepare_ingestion_observation_keys(
    session: AsyncSession,
    source_id: UUID,
    keys: tuple[tuple[str, UUID], ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
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
        session,
        source_id,
        source_fence.generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    ):
        raise RuntimeError("observation_preparation_source_changed")
    before_accepted, before_series, accepted, current = await _observation_key_snapshot(
        session,
        source_id,
        keys,
        scope.workspace_id,
    )
    existing_ids = {row.id for row in (*accepted.values(), *current.values())}
    if existing_ids:
        await session.scalars(
            select(Observation)
            .where(Observation.id.in_(existing_ids))
            .order_by(Observation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    after_accepted, after_series, _, _ = await _observation_key_snapshot(
        session, source_id, keys, scope.workspace_id
    )
    if before_accepted != after_accepted or before_series != after_series:
        raise RuntimeError("observation_preparation_identity_changed")
    return IngestionObservationPreparation(
        workspace_id=scope.workspace_id,
        source_id=source_id,
        source_generation=source_fence.generation,
        accepted=after_accepted,
        series=after_series,
    )


async def upsert_from_ingestion(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    ingestion_observation_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    external_id: str,
    provider_version: str | None,
    observed_at: datetime,
    collected_at: datetime,
    accepted_at: datetime,
    provider_scope_discriminator: str,
    measurement: WorldMeasurement,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ObservationWriteResult | None:
    """Acquire Source, D parents and Observation union, preserving ordinary optional result.

    Entry has no later domain/outbox locks. The wrapper captures current admission
    for this immediate transaction; deferred normalization uses original fences
    and explicit preparation. Invalid Source/provider returns None before writes;
    exact duplicate/current ordering and owner result fields remain unchanged.
    """
    source_fence = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if (
        source_fence is None
        or source_fence.status != "active"
        or source_fence.generation != source_generation
    ):
        return None
    access_fence = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await documents.prepare_normalized_document_keys(
        session,
        source_id,
        (external_id,),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    preparation = await prepare_ingestion_observation_keys(
        session,
        source_id,
        ((external_id, ingestion_observation_id),),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    result, _ = await upsert_from_ingestion_in_uow(
        session,
        source_id=source_id,
        source_generation=source_generation,
        ingestion_observation_id=ingestion_observation_id,
        document_id=document_id,
        document_version_id=document_version_id,
        external_id=external_id,
        provider_version=provider_version,
        observed_at=observed_at,
        collected_at=collected_at,
        accepted_at=accepted_at,
        provider_scope_discriminator=provider_scope_discriminator,
        measurement=measurement,
        preparation=preparation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    return result


async def upsert_from_ingestion_in_uow(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    ingestion_observation_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    external_id: str,
    provider_version: str | None,
    observed_at: datetime,
    collected_at: datetime,
    accepted_at: datetime,
    provider_scope_discriminator: str,
    measurement: WorldMeasurement,
    preparation: IngestionObservationPreparation,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
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
    if (
        not isinstance(preparation, IngestionObservationPreparation)
        or preparation.workspace_id != scope.workspace_id
        or preparation.source_id != source_id
        or preparation.source_generation != source_generation
    ):
        raise RuntimeError("observation_preparation_header_changed")
    pairs = tuple(
        (item.external_id, item.ingestion_observation_id) for item in preparation.accepted
    )
    pair = (external_id, ingestion_observation_id)
    expected_accepted = next(
        (
            item
            for item in preparation.accepted
            if (item.external_id, item.ingestion_observation_id) == pair
        ),
        None,
    )
    expected_series = next(
        (item for item in preparation.series if item.external_id == external_id), None
    )
    if expected_accepted is None or expected_series is None:
        raise RuntimeError("observation_preparation_key_missing")
    if not await _observation_source_proof(
        session,
        source_id,
        source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    ):
        return None, preparation
    normalized = WorldMeasurement.model_validate(measurement.model_dump(mode="python"))
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        return None, preparation
    if any(
        value.tzinfo is None or value.utcoffset() is None for value in (observed_at, collected_at)
    ):
        raise ValueError("Observation clocks must be timezone aware")
    provider_scope = await connectors.get_current_provider_scope(
        session,
        source_id,
        source_generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if (
        provider_scope is None
        or provider_scope.provider_id != normalized.provider
        or provider_scope.workspace_id != scope.workspace_id
        or provider_scope.source_id != source_id
        or provider_scope.source_generation != source_generation
        or provider_scope.discriminator != provider_scope_discriminator
    ):
        return None, preparation
    fresh_accepted, fresh_series, accepted, current = await _observation_key_snapshot(
        session,
        source_id,
        pairs,
        scope.workspace_id,
    )
    if fresh_accepted != preparation.accepted or fresh_series != preparation.series:
        raise RuntimeError("observation_preparation_identity_changed")
    if not await documents.normalized_observation_version_matches_in_uow(
        session,
        source_id=source_id,
        source_generation=source_generation,
        document_id=document_id,
        document_version_id=document_version_id,
        external_id=external_id,
        provider=normalized.provider,
        provider_scope_discriminator=provider_scope_discriminator,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    ):
        raise RuntimeError("observation_preparation_document_provenance_changed")
    digest = hashlib.sha256(
        json.dumps(
            {
                **normalized.model_dump(mode="json"),
                "observed_at": observed_at.astimezone(UTC).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    existing = accepted.get(pair)
    if existing is not None:
        if (
            existing.source_generation != source_generation
            or existing.provider != normalized.provider
            or existing.provider_scope_discriminator != provider_scope_discriminator
        ):
            raise RuntimeError("observation_preparation_accepted_lineage_changed")
        if existing.content_hash != digest:
            raise ValueError("ingestion observation was reused for different provider content")
        return ObservationWriteResult(existing.id, existing.is_current, False), preparation
    result, row = await _apply_ingestion_observation(
        session,
        source_id=source_id,
        source_generation=source_generation,
        workspace_id=scope.workspace_id,
        ingestion_observation_id=ingestion_observation_id,
        document_id=document_id,
        document_version_id=document_version_id,
        external_id=external_id,
        provider_version=provider_version,
        observed_at=observed_at,
        collected_at=collected_at,
        accepted_at=accepted_at,
        provider_scope_discriminator=provider_scope_discriminator,
        normalized=normalized,
        digest=digest,
        current=current.get(external_id),
        highest_revision=expected_series.max_revision,
    )
    successor = IngestionObservationPreparation(
        workspace_id=preparation.workspace_id,
        source_id=source_id,
        source_generation=source_generation,
        accepted=tuple(
            IngestionObservationAcceptedState(external_id, ingestion_observation_id, row.id)
            if (item.external_id, item.ingestion_observation_id) == pair
            else item
            for item in preparation.accepted
        ),
        series=tuple(
            IngestionObservationSeriesState(
                external_id,
                row.id if result.selected_current else item.current_observation_id,
                row.revision,
            )
            if item.external_id == external_id
            else item
            for item in preparation.series
        ),
    )
    after_accepted, after_series, _, _ = await _observation_key_snapshot(
        session, source_id, pairs, scope.workspace_id
    )
    if after_accepted != successor.accepted or after_series != successor.series:
        raise RuntimeError("observation_preparation_local_write_changed")
    return result, successor


async def _apply_ingestion_observation(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    workspace_id: UUID,
    ingestion_observation_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    external_id: str,
    provider_version: str | None,
    observed_at: datetime,
    collected_at: datetime,
    accepted_at: datetime,
    provider_scope_discriminator: str,
    normalized: WorldMeasurement,
    digest: str,
    current: Observation | None,
    highest_revision: int,
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
        current.accepted_at.astimezone(UTC),
        str(current.ingestion_observation_id),
    )
    row_id = uuid7()
    row = Observation(
        id=row_id,
        workspace_id=workspace_id,
        source_id=source_id,
        external_id=external_id,
        provider=normalized.provider,
        provider_scope_discriminator=provider_scope_discriminator,
        provider_version=provider_version,
        revision=highest_revision + 1,
        metric=normalized.metric,
        symbol=normalized.symbol,
        region=normalized.region,
        latitude=normalized.latitude,
        longitude=normalized.longitude,
        observed_at=observed_at,
        published_at=normalized.published_at,
        collected_at=collected_at,
        accepted_at=accepted_utc,
        value=normalized.value,
        unit=normalized.unit,
        currency=normalized.currency,
        quality=normalized.quality,
        missing_reason=normalized.missing_reason,
        timezone=normalized.timezone,
        provider_delay_seconds=None,
        is_current=is_current,
        content_hash=digest,
        document_id=document_id,
        document_version_id=document_version_id,
        ingestion_observation_id=ingestion_observation_id,
        source_generation=source_generation,
    )
    if current is not None and is_current:
        current.is_current = False
    session.add(row)
    await session.flush()
    if (
        row.id != row_id
        or row.revision != highest_revision + 1
        or row.workspace_id != workspace_id
        or row.source_id != source_id
        or row.external_id != external_id
        or row.document_id != document_id
        or row.document_version_id != document_version_id
        or row.ingestion_observation_id != ingestion_observation_id
        or row.source_generation != source_generation
        or row.content_hash != digest
        or row.is_current != is_current
    ):
        raise RuntimeError("observation_preparation_insert_changed")
    return ObservationWriteResult(row.id, is_current, is_current), row
