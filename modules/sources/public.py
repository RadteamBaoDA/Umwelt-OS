import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Integer, Select, and_, case, cast, desc, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.auth.models import Owner
from core.events import DomainEvent
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_source_change
from core.tools.schemas import ToolDestination
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import (
    ConnectorSource,
    GadgetSourceSelection,
    GadgetSourceSelectionPage,
    OperationRead,
    SourceCreate,
    SourceExportFence,
    SourceFence,
    SourceMetadataExportFence,
    SourceMetadataExportPage,
    SourceMetadataExportValidation,
    SourcePatch,
    SourceRead,
)


def _encode_source_export_cursor(snapshot_at: datetime, created_at: datetime, identifier: UUID) -> str:
    """Bind a canonical source metadata keyset position to one fixed owner snapshot."""
    payload = json.dumps(
        [1, "sources", snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_source_export_cursor(cursor: str) -> tuple[datetime, datetime, UUID]:
    """Reject oversized, noncanonical, cross-dataset, or future source export cursors."""
    try:
        if len(cursor) > 512 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 5 or values[:2] != [1, "sources"]:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(values[2]), datetime.fromisoformat(values[3])
        identifier = UUID(values[4])
        if (any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, created_at))
                or snapshot_at.isoformat() != values[2] or created_at.isoformat() != values[3]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != values[4]
                or _encode_source_export_cursor(snapshot_at, created_at, identifier) != cursor):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Source export cursor is invalid") from exc


def _source_export_scope(snapshot_at: datetime) -> tuple[ColumnElement[bool], ...]:
    """Select source rows retained at the fixed cutoff without unfinished data purges."""
    return (
        Source.created_at <= snapshot_at,
        Source.updated_at <= snapshot_at,
        Source.id.in_(export_eligible_source_ids()),
    )


def _source_export_columns() -> tuple[InstrumentedAttribute[Any], ...]:
    """Return only credential-free fields supported by the current SourceRead DTO."""
    return (
        Source.id, Source.type, Source.name, Source.provider, Source.status, Source.local_only,
        Source.last_sync_at, Source.last_success_at, Source.last_error_at, Source.last_error_code,
        Source.collected_at, Source.indexed_at, Source.collection_error_code,
        Source.processing_error_code, Source.generation, Source.retired_at,
        Source.created_at, Source.updated_at,
    )


def _source_export_read(row: Any) -> SourceRead:
    """Build one safe source record from explicit columns, never loading connector configuration."""
    return SourceRead(**row._mapping)


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
) -> SourceMetadataExportPage:
    """Return a bounded source metadata export, excluding all provider configuration and secrets."""
    if owner_id != 1 or record_kind != "sources" or not 1 <= limit <= 100:
        raise ValueError("Source export owner, kind or page limit is invalid")
    if await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        raise HTTPException(status_code=404, detail="Owner not found")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_source_export_cursor(cursor)
        position = (position_at, position_id)
    snapshot_count = int(await session.scalar(
        select(func.count()).select_from(Source).where(*_source_export_scope(snapshot_at))
    ) or 0)
    statement = select(*_source_export_columns()).where(*_source_export_scope(snapshot_at))
    if position is not None:
        statement = statement.where(tuple_(Source.created_at, Source.id) > position)
    rows = list((await session.execute(
        statement.order_by(Source.created_at, Source.id).limit(limit + 1)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [_source_export_read(row) for row in rows]
    encoded_items = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded_items)) + max(0, len(items) - 1)
    if payload_bytes > 16_777_216:
        raise HTTPException(status_code=413, detail="Source export page exceeds its byte bound")
    fences = [SourceMetadataExportFence(
        source_id=row.id, created_at=row.created_at, updated_at=row.updated_at,
        generation=row.generation, content_digest=hashlib.sha256(raw).hexdigest(),
    ) for row, raw in zip(rows, encoded_items, strict=True)]
    return SourceMetadataExportPage(
        owner_id=owner_id, record_kind="sources", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_source_export_cursor(snapshot_at, rows[-1].created_at, rows[-1].id)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[SourceMetadataExportFence],
) -> SourceMetadataExportValidation:
    """Recheck source eligibility, generation, row content, and fixed-cutoff inventory."""
    if owner_id != 1 or record_kind != "sources" or len(fences) > 100:
        raise ValueError("Source export validation input is invalid")
    if await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        return SourceMetadataExportValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    observed = int(await session.scalar(
        select(func.count()).select_from(Source).where(*_source_export_scope(snapshot_at))
    ) or 0)
    if observed != expected_snapshot_count:
        return SourceMetadataExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    source_fences = [SourceExportFence(source_id=item.source_id, generation=item.generation) for item in fences]
    if len(await filter_export_eligible_sources(session, source_fences)) != len(source_fences):
        return SourceMetadataExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = (await session.execute(
            select(*_source_export_columns()).where(
                Source.id == fence.source_id, *_source_export_scope(snapshot_at),
            )
        )).one_or_none()
        if row is None:
            return SourceMetadataExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = _source_export_read(row)
        if (item.generation != fence.generation or item.created_at != fence.created_at
                or item.updated_at != fence.updated_at
                or hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest() != fence.content_digest):
            return SourceMetadataExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return SourceMetadataExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


def export_eligible_source_ids() -> Select[UUID]:
    """Return a SQL source-ID projection excluding committed, unfinished data purges.

    The predicate is deliberately source-owned so export consumers can filter and count
    eligible rows before paging without importing SourcePurgeOperation or materializing
    the source table. Archived connector-only sources remain eligible because they have
    no data-purge operation.
    """
    pending_data_purges = select(SourcePurgeOperation.source_id).where(
        SourcePurgeOperation.status.in_(("queued", "running", "failed"))
    )
    return select(Source.id).where(~Source.id.in_(pending_data_purges))


async def filter_export_eligible_sources(
    session: AsyncSession, fences: Sequence[SourceExportFence],
) -> tuple[UUID, ...]:
    """Keep a bounded set of captured source generations with no unfinished data purge."""
    if len(fences) > 100:
        raise ValueError("Source export fence set exceeds its page limit")
    source_ids = [fence.source_id for fence in fences]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("Source export fences must have unique source IDs")
    if not source_ids:
        return ()
    generations = {fence.source_id: fence.generation for fence in fences}
    rows = (await session.execute(
        select(Source.id, Source.generation)
        .where(Source.id.in_(source_ids), Source.id.in_(export_eligible_source_ids()))
    )).all()
    return tuple(
        source_id for source_id, generation in rows
        if generations.get(source_id) == generation
    )


async def observability_quality_summary(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Count stale active scheduled sources using configured cadence and source-type defaults."""
    now = now or datetime.now(UTC)
    cadence = cast(Source.configuration["schedule_interval_minutes"].astext, Integer)
    default_cadence = case((Source.type == "rss", 15), else_=30)
    stale_sources = int(await session.scalar(select(func.count()).select_from(Source).where(
        Source.status == "active",
        Source.type.in_(("rss", "web", "api")),
        func.coalesce(Source.last_success_at, Source.created_at)
        < now - func.make_interval(0, 0, 0, 0, 0, 0, func.coalesce(cadence, default_cadence) * 120),
    )) or 0)
    return {"stale_sources": stale_sources}


@dataclass(frozen=True)
class ToolSourceRead:
    """Detached source identity and lifecycle fields safe for native tool output."""
    id: UUID
    name: str
    type: str
    status: str
    generation: int
    local_only: bool
    created_at: datetime


@dataclass(frozen=True)
class ToolSourcePage:
    """Carry a bounded detached source page and continuation cursor."""
    items: tuple[ToolSourceRead, ...]
    next_cursor: str | None


async def create_source(session: AsyncSession, payload: SourceCreate) -> Source:
    """Create a source and publish its initial state in the caller transaction."""
    source = Source(
        type=payload.type,
        name=payload.name,
        provider=payload.provider,
        local_only=payload.type == "manual",
    )
    session.add(source)
    await session.flush()
    await commit_with_replay(session, [make_source_change(source.id, source.generation, source.status)])
    await session.refresh(source)
    return source


async def ensure_demo_source(session: AsyncSession, source_id: UUID, namespace: str) -> bool:
    """Create the namespaced fictional demo source once, rejecting ID collisions."""
    inserted = await session.scalar(
        pg_insert(Source)
        .values(
            id=source_id,
            type="manual",
            name="Demo: fictional notes",
            local_only=True,
            configuration={"demo_namespace": namespace},
        )
        .on_conflict_do_nothing(index_elements=[Source.id])
        .returning(Source.id)
    )
    if inserted is not None:
        return True
    source = await session.get(Source, source_id)
    if source is None or source.configuration.get("demo_namespace") != namespace:
        raise RuntimeError("Demo source identity is occupied by another source")
    return False


async def get_source(session: AsyncSession, source_id: UUID) -> Source | None:
    """Read a source ORM record by identifier."""
    return await session.get(Source, source_id)


async def get_tool_source(
    session: AsyncSession, source_id: UUID, *, source_ids: frozenset[UUID],
    owner_all: bool = False, destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourceRead | None:
    """Read an active source projection within exact scope and destination privacy policy."""
    if not owner_all and source_id not in source_ids:
        return None
    row = (await session.execute(
        select(Source.id, Source.name, Source.type, Source.status, Source.generation,
               Source.local_only, Source.created_at)
        .where(Source.id == source_id, Source.status == "active")
    )).one_or_none()
    if row is not None and destination != ToolDestination.LOCAL and row.local_only:
        return None
    return ToolSourceRead(*row) if row else None


async def list_tool_sources(
    session: AsyncSession, *, limit: int, cursor: str | None,
    source_ids: frozenset[UUID], owner_all: bool = False,
    destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourcePage:
    """Page active, exact-scope sources after destination privacy filtering.

    Remote local-only rows are excluded before ordering, limit and cursor construction; the
    projection contains no source configuration or credentials.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Source tool page size is outside its supported bound")
    statement = select(
        Source.id, Source.name, Source.type, Source.status, Source.generation,
        Source.local_only, Source.created_at,
    ).where(Source.status == "active")
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if not owner_all:
        if not source_ids:
            return ToolSourcePage((), None)
        statement = statement.where(Source.id.in_(source_ids))
    if cursor:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(
        statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1)
    )).all())
    more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if more and page else None
    return ToolSourcePage(tuple(ToolSourceRead(*row) for row in page), next_cursor)


def _connector_source(source: Source) -> ConnectorSource:
    """Build a defensive public connector projection from a source record."""
    return ConnectorSource(
        id=source.id,
        type=source.type,
        status=source.status,
        generation=source.generation,
        configuration=deepcopy(source.configuration or {}),
        provider=source.provider,
    )


async def get_connector_source(session: AsyncSession, source_id: UUID) -> ConnectorSource | None:
    """Read fresh retained source configuration and project it for connector consumers.

    populate_existing keeps generation, status and provider scope checks current even when the
    caller's identity map already holds this source; this remains a read with no row lock.
    """
    source = await session.scalar(select(Source).where(Source.id == source_id)
                                  .execution_options(populate_existing=True))
    return _connector_source(source) if source is not None else None


async def _lock_source_row(session: AsyncSession, source_id: UUID) -> Source | None:
    """Lock and refresh a source row for transaction-serialized mutations."""
    return await session.scalar(
        select(Source)
        .where(Source.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_source_fence(session: AsyncSession, source_id: UUID) -> SourceFence | None:
    """Read a detached source eligibility fence without acquiring a database row lock.

    Owner: modules/sources
    Fields: id, status, generation, local_only
    Permissions & Deletion checks: Read-only projection; returns None if source does not exist.
    """
    source = await session.scalar(select(Source).where(
        Source.id == source_id,
    ).execution_options(populate_existing=True))
    if source is None:
        return None
    return SourceFence(
        id=source.id, status=source.status, generation=source.generation, local_only=source.local_only
    )


def ingestion_lifecycle_projection() -> Select[UUID, str, int]:
    """Return the minimal source lifecycle read projection for ingestion retry aggregation.

    Cross-module callers may correlate ingestion-owned retry records against the
    current source identity, status, and generation. Configuration and content
    remain private, and this projection grants no write authority.
    """
    return select(Source.id, Source.status, Source.generation)


async def lock_source(session: AsyncSession, source_id: UUID) -> SourceFence | None:
    """Acquire the source lock and return the narrow lifecycle fence contract."""
    source = await _lock_source_row(session, source_id)
    if source is None:
        return None
    return SourceFence(
        id=source.id, status=source.status, generation=source.generation, local_only=source.local_only
    )


async def lock_retained_evidence_source(session: AsyncSession, source_id: UUID) -> SourceFence | None:
    """Share-lock a source while confirming retained data remains eligible for owner reads/effects.

    Paused and connector-only archived sources remain eligible. A queued, running, or failed
    with-data purge revokes eligibility immediately, even while immutable document rows linger.
    The PostgreSQL ``FOR SHARE`` lock is compatible with other evidence readers but conflicts with
    Source lifecycle ``FOR UPDATE`` mutations. Acquire bounded Source sets in UUID order before
    Documents or mutable owner rows, then hold this lock through the caller's publication commit.
    """
    source = await session.scalar(
        select(Source).where(Source.id == source_id).with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if source is None:
        return None
    eligible_ids = await filter_export_eligible_sources(
        session, [SourceExportFence(source_id=source.id, generation=source.generation)],
    )
    if source.id not in eligible_ids:
        return None
    return SourceFence(
        id=source.id, status=source.status, generation=source.generation, local_only=source.local_only,
    )


async def filter_active_source_ids(session: AsyncSession, source_ids: Sequence[UUID]) -> tuple[UUID, ...]:
    """Return only owner sources that are active and still eligible for current reads."""
    if not source_ids or len(source_ids) > 32:
        return ()
    rows = await session.scalars(
        select(Source.id).where(Source.id.in_(source_ids), Source.status == "active", Source.retired_at.is_(None))
    )
    return tuple(rows.all())



async def lock_source_for_document(session: AsyncSession, source_id: UUID) -> None:
    """Validate the source and hold its lock until the caller's transaction ends."""
    source = await _lock_source_row(session, source_id)
    if source is None:
        raise LookupError("Source not found")
    if source.status != "active":
        raise ValueError("Cannot add documents to an inactive source")


async def set_connector_configuration(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    configuration: dict[str, object],
    *,
    allow_paused: bool = False,
) -> ConnectorSource | None:
    """Replace connector configuration when lifecycle and generation fences match."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status == "archived" or source.generation != expected_generation:
        return None
    if source.status != "active" and not (allow_paused and source.status == "paused"):
        return None
    source.generation += 1
    source.configuration = deepcopy(configuration)
    await session.flush()
    return _connector_source(source)


async def record_collection_started(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime
) -> bool:
    """Record a collection start only for the active expected source generation."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    source.last_sync_at = at
    source.collected_at = at
    await session.flush()
    return True


async def record_collection_result(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    at: datetime,
    error_code: str | None,
    *,
    no_changes: bool = False,
) -> bool:
    """Record collection success or error if the source generation is current."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    if error_code is None:
        source.last_success_at = at
        source.last_error_code = None
        source.collection_error_code = None
        if no_changes:
            source.last_sync_at = at
            source.collected_at = at
            source.last_error_at = None
    else:
        source.collection_error_code = error_code
        source.last_error_code = error_code
        source.last_error_at = at
    await session.flush()
    return True


async def record_processing_result(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, error_code: str | None
) -> bool:
    """Record processing success or error if the source generation is current."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status != "active" or source.generation != expected_generation:
        return False
    if error_code is None:
        source.last_success_at = at
        source.last_error_code = None
        source.processing_error_code = None
    else:
        source.processing_error_code = error_code
        source.last_error_code = error_code
        source.last_error_at = at
    await session.flush()
    return True


async def list_sources(
    session: AsyncSession, limit: int, cursor: str | None
) -> tuple[list[Source], str | None]:
    """Return a descending keyset page of sources and its continuation cursor."""
    statement = select(Source).order_by(desc(Source.created_at), desc(Source.id))
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return rows, next_cursor


async def get_gadget_sources(
    session: AsyncSession, source_ids: tuple[UUID, ...]
) -> tuple[GadgetSourceSelection, ...]:
    """Project up to 32 unique source IDs in request order for an authorized dashboard caller.

    The caller must already have owner authorization; this internal query does
    not grant access to external agents. Missing IDs are omitted. The detached
    projection excludes configuration, credentials, scopes, and content, and
    does not prove provider item-level authorization.
    """
    if len(source_ids) > 32 or len(set(source_ids)) != len(source_ids):
        raise ValueError("At most 32 distinct source IDs may be projected")
    if not source_ids:
        return ()

    rows = await session.execute(
        select(
            Source.id,
            Source.name,
            Source.type,
            Source.provider,
            Source.status,
            Source.generation,
            Source.local_only,
        ).where(Source.id.in_(source_ids))
    )
    by_id = {
        row.id: GadgetSourceSelection(
            id=row.id,
            name=row.name,
            type=row.type,
            provider=row.provider,
            status=row.status,
            generation=row.generation,
            local_only=row.local_only,
        )
        for row in rows
    }
    return tuple(by_id[source_id] for source_id in source_ids if source_id in by_id)


async def list_active_gadget_sources(
    session: AsyncSession, *, limit: int = 32, cursor: str | None = None,
) -> GadgetSourceSelectionPage:
    """Page detached active-source identities for owner News selection and bounded catch-up.

    This contract contains only the existing gadget-safe source fields, applies
    active lifecycle and stable creation/ID keyset filters, and excludes scope,
    configuration, credentials and provider item policy. Callers must still use
    Documents current-version/scope fences for each item; this is not item-level
    authorization and does not grant agent access.
    """
    if not 1 <= limit <= 32:
        raise ValueError("Active source projection limit must be between 1 and 32")
    statement = select(
        Source.id, Source.name, Source.type, Source.provider, Source.status,
        Source.generation, Source.local_only, Source.created_at,
    ).where(Source.status == "active")
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    items = tuple(GadgetSourceSelection(
        id=row.id, name=row.name, type=row.type, provider=row.provider,
        status=row.status, generation=row.generation, local_only=row.local_only,
    ) for row in rows)
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if more and rows else None
    return GadgetSourceSelectionPage(items=items, next_cursor=next_cursor)


async def list_gadget_sources(
    session: AsyncSession, limit: int = 50, cursor: str | None = None
) -> GadgetSourceSelectionPage:
    """Return an authorized caller's bounded source-selection page using created-at/ID keyset order.

    Only dashboard selection fields are queried, so connector configuration,
    credentials, scopes, and source content never enter this detached page.
    Authorization remains the route caller's responsibility; this projection
    does not establish provider item-level authorization.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Source selection page limit must be between 1 and 100")

    statement = select(
        Source.id,
        Source.name,
        Source.type,
        Source.provider,
        Source.status,
        Source.generation,
        Source.local_only,
        Source.created_at,
    ).order_by(desc(Source.created_at), desc(Source.id))
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    items = tuple(
        GadgetSourceSelection(
            id=row.id,
            name=row.name,
            type=row.type,
            provider=row.provider,
            status=row.status,
            generation=row.generation,
            local_only=row.local_only,
        )
        for row in rows
    )
    return GadgetSourceSelectionPage(items=items, next_cursor=next_cursor)


async def update_source(
    session: AsyncSession, source: Source, payload: SourcePatch
) -> Source | None:
    """Apply a locked source patch and reconcile GitHub hint reservations on lifecycle changes.

    Pausing or archiving fences collection before pending hints are terminalized in this same
    transaction. Explicit reactivation rearms only hints whose current grant and provisioning
    binding match the new source generation; it never resumes a stale OAuth binding.
    """
    locked_source = await session.scalar(
        select(Source)
        .where(Source.id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_source is None:
        return None
    source = locked_source
    changed = False
    if source.status == "archived" and payload.status not in (None, "archived"):
        raise ValueError("Archived sources cannot be reactivated")
    if "name" in payload.model_fields_set:
        value = payload.name or ""
        changed = changed or source.name != value
        source.name = value
    if "status" in payload.model_fields_set:
        next_status = payload.status or ""
        if next_status != source.status:
            changed = True
            source.generation += 1
            source.status = next_status
            source.retired_at = datetime.now(UTC) if next_status in {"paused", "archived"} else None
            if next_status in {"paused", "archived"}:
                await _fence_connector_source(session, source)
            if source.provider == "github":
                await _reconcile_github_hint_lifecycle(
                    session, source, active=next_status == "active",
                )
    drafts = [make_source_change(source.id, source.generation, source.status)] if changed else []
    await commit_with_replay(session, drafts)
    await session.refresh(source)
    return source


async def pause_source_for_connector(
    session: AsyncSession, source_id: UUID
) -> ConnectorSource | None:
    """Pause and fence a connector source, disposing pending GitHub hints in caller transaction."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status == "archived":
        return None
    if source.status != "paused":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(session, source)
    if source.provider == "github":
        await _reconcile_github_hint_lifecycle(session, source, active=False)
    await session.flush()
    return _connector_source(source)


async def archive_source(
    session: AsyncSession, source_id: UUID
) -> Source | None:
    """Archive and fence a source, releasing its pending GitHub hint reservations."""
    source = await _lock_source_row(session, source_id)
    if source is None:
        return None
    changed = source.status != "archived"
    if source.status != "archived":
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(session, source)
    if source.provider == "github":
        await _reconcile_github_hint_lifecycle(session, source, active=False)
    drafts = [make_source_change(source.id, source.generation, source.status)] if changed else []
    await commit_with_replay(session, drafts)
    await session.refresh(source)
    return source


async def start_source_purge(
    session: AsyncSession, source_id: UUID
) -> SourcePurgeOperation | None:
    """Queue source purge after generation, connector, and browser admission fences commit.

    New operations carry no raw URI snapshot: Documents creates durable per-document cleanup
    receipts under its publication identity locks before the canonical cascade. The legacy
    JSON column remains readable only for explicit compatibility repair.
    """
    source = await _lock_source_row(session, source_id)
    if source is None:
        return None
    current = await session.scalar(
        select(SourcePurgeOperation).where(
            SourcePurgeOperation.source_id == source_id,
        ).order_by(SourcePurgeOperation.created_at.desc()).limit(1)
    )
    if current is not None and (source.status == "archived" or current.status in {"queued", "running"}):
        return current

    changed = source.status != "archived"
    if changed:
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    operation = SourcePurgeOperation(
        source_id=source_id, generation=source.generation, raw_uris=[],
        pending_owner_codes=["documents"],
    )
    session.add(operation)
    await session.flush()
    from modules.ingestion import public as ingestion
    await _fence_connector_source(session, source)
    if source.provider == "github":
        await _reconcile_github_hint_lifecycle(session, source, active=False)
    now = datetime.now(UTC)
    event = DomainEvent(
        id=uuid4(), type="source.purge.requested", version=1, occurred_at=now,
        producer="modules.sources", payload={"operation_id": str(operation.id)},
    )
    await ingestion.publish_event(session, event)
    drafts = [make_source_change(source.id, source.generation, source.status, operation_id=operation.id)]
    await commit_with_replay(session, drafts)
    await session.refresh(operation)
    return operation


# Source-local Memory coverage that can never improve without new evidence: durable unavailable.
SOURCE_MEMORY_TERMINAL_CODES = frozenset({"evidence_identity_unavailable", "legacy_provenance_unresolved"})


def _open_coverage_operations() -> tuple[ColumnElement[bool], ...]:
    """Select canonical-complete operations whose full-copy status can still change.

    Terminal-unavailable Memory coverage is excluded so a permanent gap never starves queued work
    or hot-loops; it only reopens through a new, explicit purge decision.
    """
    return (
        SourcePurgeOperation.documents_status == "deleted",
        SourcePurgeOperation.status != "succeeded",
        ~and_(
            SourcePurgeOperation.memory_status == "failed",
            SourcePurgeOperation.memory_error_code.in_(SOURCE_MEMORY_TERMINAL_CODES),
        ),
    )


async def list_source_purge_observer_ids(
    session: AsyncSession, source_id: UUID, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return <=100 exact unfinished purge operation IDs for one Source, keyset by ID.

    Documents uses this detached identity list to wake observers of a historical (unlinked)
    receipt without touching Source models. No ordering of unrelated operations is exposed.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Source observer page size must be between 1 and 100")
    statement = select(SourcePurgeOperation.id).where(
        SourcePurgeOperation.source_id == source_id, *_open_coverage_operations(),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple((await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all())


async def pending_source_coverage_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return one bounded keyset page of purge operations needing coverage reconciliation."""
    if not 1 <= limit <= 100:
        raise ValueError("Source coverage reconciliation page size must be between 1 and 100")
    # A failed purge whose Memory stage is done is terminal for polling; it re-settles only through
    # Documents/observer wakeups, which still select it via _open_coverage_operations.
    statement = select(SourcePurgeOperation.id).where(
        *_open_coverage_operations(),
        ~and_(
            SourcePurgeOperation.status == "failed",
            SourcePurgeOperation.memory_status == "succeeded",
            SourcePurgeOperation.memory_cache_pending.is_(False),
        ),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple((await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all())


async def source_data_purge_exists(session: AsyncSession, source_id: UUID) -> bool:
    """Report whether any data purge (in any state) ever fenced this Source.

    Export eligibility reopens when a purge succeeds; copied-evidence producers must not, so they
    use this durable fence instead. Callers hold the Source lock, which serializes this read with
    ``start_source_purge``.
    """
    return await session.scalar(
        select(SourcePurgeOperation.id).where(SourcePurgeOperation.source_id == source_id).limit(1)
    ) is not None


async def read_source_purge_operation(
    session: AsyncSession,
    operation_id: UUID,
) -> OperationRead | None:
    """Return the allowlisted public progress projection for one Source purge receipt.

    The exact Source-owned ID lookup exposes aggregate stage/count fields only; it never returns
    legacy URI JSON, child receipt IDs, event payloads, or owner-private cleanup data.
    """
    operation = await session.scalar(select(SourcePurgeOperation).where(
        SourcePurgeOperation.id == operation_id,
    ))
    if operation is None:
        return None
    return OperationRead(
        operation_id=operation.id,
        source_id=operation.source_id,
        status=operation.status,
        error_code=operation.error_code,
        documents_status=operation.documents_status,
        pending_child_count=operation.pending_child_count,
        failed_child_count=operation.failed_child_count,
        pending_owner_codes=list(operation.pending_owner_codes),
        memory_status=operation.memory_status,
        memory_error_code=operation.memory_error_code,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


async def _fence_connector_source(session: AsyncSession, source: Source) -> None:
    """Revoke credentials and browser opt-in, then invalidate outstanding collection work."""
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion

    await ingestion.revoke_source_credentials(session, source.id)
    await connectors.invalidate_agent_browser_grant_in_uow(session, source.id)
    from modules.tools.public import purge_browser_results_in_uow
    await purge_browser_results_in_uow(session, source_ids=[source.id])
    await connectors.fence_source_collection(
        session,
        SourceFence(
            id=source.id,
            status=source.status,
            generation=source.generation,
            local_only=source.local_only,
        ),
    )


async def _reconcile_github_hint_lifecycle(
    session: AsyncSession, source: Source, *, active: bool,
) -> None:
    """Continue the held source/provisioning lifecycle transaction into GitHub hint ownership.

    The connector owner acquires the GitHub grant before hints and capacity. A source is rearmed
    only for an explicit activation carrying a current verified grant; access-loss pauses stay
    paused until the owner reconnects and explicitly resumes the source.
    """
    from modules.connectors import public as connectors

    await connectors.reconcile_github_source_hints_lifecycle(
        session, source_id=source.id, source_generation=source.generation, active=active,
    )
