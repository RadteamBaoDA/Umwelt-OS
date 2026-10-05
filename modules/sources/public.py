from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Integer, case, cast, desc, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from core.events import DomainEvent
from core.realtime import commit_with_replay, make_source_change
from core.tools.schemas import ToolDestination
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import (
    ConnectorSource,
    GadgetSourceSelection,
    GadgetSourceSelectionPage,
    SourceCreate,
    SourceFence,
    SourcePatch,
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
    """Read and project a source for connector consumers."""
    source = await session.get(Source, source_id)
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
    source = await session.get(Source, source_id)
    if source is None:
        return None
    return SourceFence(
        id=source.id, status=source.status, generation=source.generation, local_only=source.local_only
    )


def ingestion_lifecycle_projection():
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
    source = await session.scalar(
        select(Source)
        .where(Source.id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None:
        return None
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
    """Create or reuse purge work after fencing and disposing source-owned GitHub hints."""
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
    operation = SourcePurgeOperation(source_id=source_id, generation=source.generation, raw_uris=[])
    session.add(operation)
    await session.flush()
    from modules.ingestion import public as ingestion
    from modules.knowledge.documents import public as documents

    operation.raw_uris = sorted(await documents.raw_uris(session, source_id))
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
