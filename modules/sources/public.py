from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import desc, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from core.events import DomainEvent
from core.realtime import commit_with_replay, make_source_change
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import ConnectorSource, SourceCreate, SourceFence, SourcePatch


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


def _connector_source(source: Source) -> ConnectorSource:
    """Build a defensive public connector projection from a source record."""
    return ConnectorSource(
        id=source.id,
        type=source.type,
        status=source.status,
        generation=source.generation,
        configuration=deepcopy(source.configuration or {}),
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


async def lock_source(session: AsyncSession, source_id: UUID) -> SourceFence | None:
    """Acquire the source lock and return the narrow lifecycle fence contract."""
    source = await _lock_source_row(session, source_id)
    if source is None:
        return None
    return SourceFence(
        id=source.id, status=source.status, generation=source.generation, local_only=source.local_only
    )



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


async def update_source(
    session: AsyncSession, source: Source, payload: SourcePatch
) -> Source | None:
    """Apply a locked source patch, incrementing generation on lifecycle changes."""
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
    drafts = [make_source_change(source.id, source.generation, source.status)] if changed else []
    await commit_with_replay(session, drafts)
    await session.refresh(source)
    return source


async def pause_source_for_connector(
    session: AsyncSession, source_id: UUID
) -> ConnectorSource | None:
    """Pause a connector source within the caller's transaction."""
    source = await _lock_source_row(session, source_id)
    if source is None or source.status == "archived":
        return None
    if source.status != "paused":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(session, source)
    await session.flush()
    return _connector_source(source)


async def archive_source(
    session: AsyncSession, source_id: UUID
) -> Source | None:
    """Archive and fence a source, preventing future collection work."""
    source = await _lock_source_row(session, source_id)
    if source is None:
        return None
    changed = source.status != "archived"
    if source.status != "archived":
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(session, source)
    drafts = [make_source_change(source.id, source.generation, source.status)] if changed else []
    await commit_with_replay(session, drafts)
    await session.refresh(source)
    return source


async def start_source_purge(
    session: AsyncSession, source_id: UUID
) -> SourcePurgeOperation | None:
    """Create or reuse durable purge work after archiving and fencing the source."""
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
    """Revoke source credentials and invalidate outstanding connector collection."""
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion

    await ingestion.revoke_source_credentials(session, source.id)
    await connectors.fence_source_collection(
        session,
        SourceFence(
            id=source.id,
            status=source.status,
            generation=source.generation,
            local_only=source.local_only,
        ),
    )
