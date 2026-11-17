"""Bounded observation list and geospatial reads behind ``modules.knowledge.observations.public``."""

# isort: split
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    String,
    and_,
    not_,
    or_,
    select,
    tuple_,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.errors import NotFoundError
from core.pagination import (
    decode_fingerprinted_cursor,
    encode_fingerprinted_cursor,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors import public as connectors
from modules.knowledge.documents import public as documents
from modules.knowledge.observations._common import (
    _admit,
)
from modules.knowledge.observations._ingest import (
    ObservationScopeRead,
)
from modules.knowledge.observations.models import Observation
from modules.knowledge.observations.schemas import (
    GeospatialObservationPage,
    GeospatialObservationRead,
    ObservationQuery,
)
from modules.sources import public as sources


def _cursor_fingerprint(
    query: ObservationQuery,
    scopes: tuple[ObservationScopeRead, ...],
    *,
    access_fence: AccessFence,
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
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Decode one bounded cursor and reject malformed or cross-filter reuse.

    Delegates to ``core.pagination.decode_fingerprinted_cursor`` (wire-compatible with cursors issued by
    ``list_observations``); every failure is HTTP 422 "Observation cursor is invalid".
    """
    return decode_fingerprinted_cursor(cursor, fingerprint, detail="Observation cursor is invalid")


def _window_statement(
    query: ObservationQuery,
    workspace_id: UUID,
    source_scope_filters: tuple[Any, ...],
) -> Any:
    """Build the filtered observation window select (no ordering, paging or evidence checks).

    Default mode reads current rows only. ``query.history`` also reads superseded revisions (kept until the
    existing retention purge removes non-current rows past the source cutoff) but hides a row when a later
    accepted row of the same series point (source, provider, scope discriminator, metric, symbol, region, unit, coordinates,
    observed_at) exists, so a corrected period never counts twice. "Later" uses the current-selection key
    (accepted_at, str(ingestion_observation_id)), so exact accepted_at ties pick the row that is_current picks.
    Same scope filters and bounds apply.
    """
    statement = select(Observation).where(
        Observation.workspace_id == workspace_id,
        or_(*source_scope_filters),
        Observation.observed_at >= query.from_at,
        Observation.observed_at < query.to_at,
    )
    if not query.history:
        return statement.where(Observation.is_current.is_(True))
    newer = aliased(Observation)
    superseded = (
        select(1)
        .where(
            newer.workspace_id == Observation.workspace_id,
            newer.source_id == Observation.source_id,
            newer.source_generation == Observation.source_generation,
            newer.provider == Observation.provider,
            newer.provider_scope_discriminator.is_not_distinct_from(
                Observation.provider_scope_discriminator
            ),
            newer.metric == Observation.metric,
            newer.observed_at == Observation.observed_at,
            newer.symbol.is_not_distinct_from(Observation.symbol),
            newer.region.is_not_distinct_from(Observation.region),
            newer.unit.is_not_distinct_from(Observation.unit),
            newer.latitude.is_not_distinct_from(Observation.latitude),
            newer.longitude.is_not_distinct_from(Observation.longitude),
            # Same key as current selection (accepted_at, str(ingestion id)) so exact ties agree with is_current.
            tuple_(newer.accepted_at, sql_cast(newer.ingestion_observation_id, String))
            > tuple_(
                Observation.accepted_at, sql_cast(Observation.ingestion_observation_id, String)
            ),
        )
        .exists()
    )
    return statement.where(not_(superseded))


async def _current_source_scopes(
    session: AsyncSession,
    query: ObservationQuery,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[ObservationScopeRead, ...], dict[UUID, object]]:
    """Resolve each requested source (sorted by id) to its current provider scope snapshot.

    Returns the scope reads plus the raw snapshots keyed by source id. Raises 404 "One or more sources are
    unavailable" if any source has no current snapshot (inactive or stale generation counts as ``-1``).
    """
    scopes: list[ObservationScopeRead] = []
    current_scopes: dict[UUID, object] = {}
    for source_id in sorted(query.source_ids, key=str):
        source = await sources.get_connector_source(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        snapshot = await connectors.get_current_provider_scope(
            session,
            source_id,
            source.generation if source is not None and source.status == "active" else -1,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if snapshot is None:
            raise NotFoundError("One or more sources are unavailable")
        current_scopes[source_id] = snapshot
        scopes.append(
            ObservationScopeRead(
                source_id=snapshot.source_id,
                source_generation=snapshot.source_generation,
                provider=snapshot.provider_id,
                discriminator=snapshot.discriminator,
            )
        )
    return tuple(scopes), current_scopes


def _filtered_window(
    query: ObservationQuery, workspace_id: UUID, scopes: tuple[ObservationScopeRead, ...]
) -> Any:
    """Build the observation window select with the query's source-scope, geo, metric, symbol and region filters."""
    source_scope_filters = tuple(
        and_(
            Observation.source_id == item.source_id,
            Observation.source_generation == item.source_generation,
            Observation.provider == item.provider,
            Observation.provider_scope_discriminator == item.discriminator,
        )
        for item in scopes
    )
    statement = _window_statement(query, workspace_id, source_scope_filters)
    if query.geospatial_only:
        # Do not infer point coordinates from a location label; only provider-recorded points map.
        statement = statement.where(
            Observation.latitude.is_not(None),
            Observation.longitude.is_not(None),
        )
    if query.metrics:
        statement = statement.where(Observation.metric.in_(query.metrics))
    if query.symbols:
        statement = statement.where(Observation.symbol.in_(query.symbols))
    if query.regions:
        statement = statement.where(Observation.region.in_(query.regions))
    return statement


@dataclass
class _ScanResult:
    """Outcome of the bounded evidence scan: kept rows, their version numbers and truncation state."""

    page: list[Observation]
    version_numbers: dict[UUID, int]
    last_scanned: Observation | None
    truncated: bool


async def _scan_evidence_page(
    session: AsyncSession,
    statement: Any,
    query: ObservationQuery,
    current_scopes: dict[UUID, object],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> _ScanResult:
    """Scan newest-first batches (<=256 rows, <=2,048 scanned) keeping rows whose evidence version is allowed.

    Stops once ``query.limit + 1`` rows are kept (the extra row proves another page), the window is exhausted,
    or the scan ceiling is reached (``truncated``). Each batch advances the keyset bound past its last row.
    """
    result = _ScanResult(page=[], version_numbers={}, last_scanned=None, truncated=False)
    scanned = 0
    while scanned < 2_048 and len(result.page) <= query.limit:
        batch = tuple(
            (
                await session.scalars(
                    statement.order_by(Observation.observed_at.desc(), Observation.id.desc()).limit(
                        min(256, 2_048 - scanned)
                    )
                )
            ).all()
        )
        if not batch:
            break
        candidates = tuple(
            documents.ObservationEvidenceCandidate(
                observation_id=row.id,
                source_id=row.source_id,
                source_generation=row.source_generation,
                provider=row.provider,
                provider_scope_discriminator=row.provider_scope_discriminator,
                external_id=row.external_id,
                document_id=row.document_id,
                document_version_id=row.document_version_id,
            )
            for row in batch
        )
        allowed = await documents.current_observation_evidence_versions(
            session,
            candidates,
            current_scopes,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            include_historical=query.history,
        )
        for row in batch:
            if row.id in allowed:
                result.page.append(row)
                result.version_numbers[row.id] = allowed[row.id]
                if len(result.page) > query.limit:
                    break
        scanned += len(batch)
        result.last_scanned = batch[-1]
        statement = statement.where(
            tuple_(Observation.observed_at, Observation.id)
            < tuple_(result.last_scanned.observed_at, result.last_scanned.id)
        )
        if len(batch) < min(256, 2_048 - (scanned - len(batch))):
            break
        if scanned >= 2_048:
            result.truncated = True
    return result


async def list_observations(
    session: AsyncSession,
    query: ObservationQuery,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[tuple[Observation, ...], str | None, bool, dict[UUID, int]]:
    """Read bounded evidence points after owner admission and workspace/source filters.

    Default: current revisions backed by the current document version. With ``query.history`` also
    superseded revisions (deduped per series point by ``_window_statement``) backed by any version of
    the same document; scope, source generation, provider identity and provenance fences are unchanged.
    Returns (rows, next cursor, scan truncated, document version number per row).
    """
    access_fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    scopes, current_scopes = await _current_source_scopes(
        session,
        query,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    statement = _filtered_window(query, scope.workspace_id, scopes)
    fingerprint = _cursor_fingerprint(query, scopes, access_fence=access_fence)
    if cursor:
        stamp, row_id = _decode_cursor(cursor, fingerprint)
        statement = statement.where(
            tuple_(Observation.observed_at, Observation.id) < tuple_(stamp, row_id)
        )
    scan = await _scan_evidence_page(
        session,
        statement,
        query,
        current_scopes,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    has_more = len(scan.page) > query.limit
    page = scan.page[: query.limit]
    page_version_numbers = {row.id: scan.version_numbers[row.id] for row in page}
    next_cursor = None
    if has_more and page:
        next_cursor = encode_fingerprinted_cursor(fingerprint, page[-1].observed_at, page[-1].id)
    elif scan.truncated and scan.last_scanned is not None:
        next_cursor = encode_fingerprinted_cursor(
            fingerprint, scan.last_scanned.observed_at, scan.last_scanned.id
        )
    return tuple(page), next_cursor, scan.truncated, page_version_numbers


async def list_geospatial_observations(
    session: AsyncSession,
    query: ObservationQuery,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
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
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        snapshot = await connectors.get_current_provider_scope(
            session,
            source_id,
            source.generation if source is not None and source.status == "active" else -1,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        # Only the existing weather adapter declares point coordinates; market symbols have none.
        if snapshot is None or snapshot.provider_id != "open_meteo":
            omitted += 1
        else:
            eligible.append(source_id)
    if not eligible:
        return GeospatialObservationPage(
            items=[],
            next_cursor=None,
            truncated=False,
            omitted_source_count=omitted,
        )
    scoped_query = query.model_copy(
        update={
            "source_ids": eligible,
            "geospatial_only": True,
        }
    )
    rows, next_cursor, truncated, version_numbers = await list_observations(
        session,
        scoped_query,
        cursor,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    version_numbers_by_observation = version_numbers
    items = [
        GeospatialObservationRead.model_validate(
            {
                **{
                    key: getattr(row, key)
                    for key in GeospatialObservationRead.model_fields
                    if hasattr(row, key)
                },
                "document_version_number": version_numbers_by_observation.get(row.id),
            }
        )
        for row in rows
    ]
    return GeospatialObservationPage(
        items=items,
        next_cursor=next_cursor,
        truncated=truncated,
        omitted_source_count=omitted,
    )
