"""Source and gadget-Source listing reads."""

from uuid import UUID

from sqlalchemy import desc, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.sources._export import (
    _cursor_fingerprint,
    _decode_source_page_cursor,
    _encode_source_page_cursor,
)
from modules.sources._scope import _admit_source_scope, _source_scope
from modules.sources.models import Source
from modules.sources.schemas import GadgetSourceSelection, GadgetSourceSelectionPage


async def list_sources(
    session: AsyncSession,
    limit: int,
    cursor: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[list[Source], str | None]:
    """Return an owner-scoped bounded descending page; reject foreign/unbound cursors."""
    access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not 1 <= limit <= 100:
        raise ValueError("Source page limit must be between 1 and 100")
    fingerprint = _cursor_fingerprint(access, scope, "sources", "created_at,id:desc", limit)
    statement = (
        select(Source)
        .where(*_source_scope(scope))
        .order_by(desc(Source.created_at), desc(Source.id))
    )
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint)
        if has_more and rows
        else None
    )
    return rows, next_cursor


async def get_gadget_sources(
    session: AsyncSession,
    source_ids: tuple[UUID, ...],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[GadgetSourceSelection, ...]:
    """Project up to 32 unique source IDs in request order for an authorized dashboard caller.

    Current workspace owner admission precedes the explicit ID intersection;
    foreign and missing IDs are omitted. The detached
    projection excludes configuration, credentials, scopes, and content, and
    does not prove provider item-level authorization.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
        ).where(Source.id.in_(source_ids), *_source_scope(scope))
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
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    limit: int = 32,
    cursor: str | None = None,
) -> GadgetSourceSelectionPage:
    """Page detached active-source identities for owner News selection and bounded catch-up.

    This contract contains only the existing gadget-safe source fields, applies
    active lifecycle and stable creation/ID keyset filters, and excludes scope,
    configuration, credentials and provider item policy. Callers must still use
    Documents current-version/scope fences for each item; this is not item-level
    authorization and does not grant agent access.
    """
    access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    fingerprint = _cursor_fingerprint(
        access, scope, "active-gadget-sources", "created_at,id:desc", limit
    )
    if not 1 <= limit <= 32:
        raise ValueError("Active source projection limit must be between 1 and 32")
    statement = select(
        Source.id,
        Source.name,
        Source.type,
        Source.provider,
        Source.status,
        Source.generation,
        Source.local_only,
        Source.created_at,
    ).where(Source.status == "active", *_source_scope(scope))
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list(
        (
            await session.execute(
                statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
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
    next_cursor = (
        _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint)
        if more and rows
        else None
    )
    return GadgetSourceSelectionPage(items=items, next_cursor=next_cursor)


async def list_gadget_sources(
    session: AsyncSession,
    limit: int = 50,
    cursor: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> GadgetSourceSelectionPage:
    """Return an authorized caller's bounded source-selection page using created-at/ID keyset order.

    Only dashboard selection fields are queried, so connector configuration,
    credentials, scopes, and source content never enter this detached page.
    Current workspace owner admission is required before paging; this projection
    does not establish provider item-level authorization.
    """
    access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    fingerprint = _cursor_fingerprint(access, scope, "gadget-sources", "created_at,id:desc", limit)
    if not 1 <= limit <= 100:
        raise ValueError("Source selection page limit must be between 1 and 100")

    statement = (
        select(
            Source.id,
            Source.name,
            Source.type,
            Source.provider,
            Source.status,
            Source.generation,
            Source.local_only,
            Source.created_at,
            Source.last_success_at,
        )
        .where(*_source_scope(scope))
        .order_by(desc(Source.created_at), desc(Source.id))
    )
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint)
        if has_more and rows
        else None
    )
    items = tuple(
        GadgetSourceSelection(
            id=row.id,
            name=row.name,
            type=row.type,
            provider=row.provider,
            status=row.status,
            generation=row.generation,
            local_only=row.local_only,
            last_success_at=row.last_success_at,
        )
        for row in rows
    )
    return GadgetSourceSelectionPage(items=items, next_cursor=next_cursor)


async def list_active_source_kinds(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> tuple[tuple[UUID, str | None, str], ...]:
    """Return ``(id, provider, type)`` for every active source in scope, unpaged and metadata-only.

    Requires owner admission like the selection page; used for exact renderer compatibility counts.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    # ponytail: full scan of active sources, single-user workspace; page if counts get slow
    rows = (
        await session.execute(
            select(Source.id, Source.provider, Source.type).where(
                Source.status == "active", *_source_scope(scope)
            )
        )
    ).all()
    return tuple((row.id, row.provider, row.type) for row in rows)
