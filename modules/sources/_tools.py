"""Tool-facing Source reads, Source creation and system (chat attachments, uploads) Source helpers."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import commit_with_replay, make_source_change
from core.tools.schemas import ToolDestination
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.sources._export import (
    _cursor_fingerprint,
    _decode_source_page_cursor,
    _encode_source_page_cursor,
)
from modules.sources._scope import _admit_source_scope, _source_scope
from modules.sources.models import Source
from modules.sources.schemas import SourceCreate


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


async def create_source(
    session: AsyncSession,
    payload: SourceCreate,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source:
    """Create in the owner workspace after access locks, preserving wrapper replay/commit.

    No network I/O occurs; manual imports and personal health, home, Google, Notion, and Slack
    sources default to local-only. An optional captured fence rejects stale deferred configuration;
    replay reuses the locked access fence without reacquiring earlier locks.
    """
    access_fence = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=expected_access_fence,
    )
    source = Source(
        workspace_id=scope.workspace_id,
        type=payload.type,
        name=payload.name,
        provider=payload.provider,
        local_only=payload.type == "manual"
        or payload.provider
        in {
            "health",
            "home_assistant",
            "google_mail",
            "google_calendar",
            "google_drive",
            "notion",
            "slack",
        },
        graph_enabled=payload.graph_enabled,
        rag_index_enabled=payload.rag_index_enabled,
    )
    session.add(source)
    await session.flush()
    await commit_with_replay(
        session,
        [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def ensure_demo_source(
    session: AsyncSession,
    source_id: UUID,
    namespace: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Stage a verified demo owner's Source once under access locks, rejecting foreign IDs.

    No commit or default-owner fallback; namespace and workspace must both match on collision.
    Caller proves demo-owner lineage and supplies its configured flag/current revision.
    An existing Source must match the bound current generation, including generations above 1;
    only a missing Source may be created at generation 1, never resurrected at a later epoch.
    """
    await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
        expected=expected_access_fence,
    )
    if isinstance(scope, InternalJobScope) and scope.source_id not in (None, source_id):
        raise ValueError("Demo Source identity does not match its scope")
    source = await session.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if source is not None:
        if source.configuration.get("demo_namespace") != namespace:
            raise RuntimeError("Demo source identity is occupied by another source")
        return False
    if (
        isinstance(scope, InternalJobScope)
        and scope.source_id is not None
        and (scope.source_generation != 1)
    ):
        raise ValueError("Demo Source identity must match its creation generation")
    inserted = await session.scalar(
        pg_insert(Source)
        .values(
            id=source_id,
            workspace_id=scope.workspace_id,
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
    source = await session.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )
    if source is None or source.configuration.get("demo_namespace") != namespace:
        raise RuntimeError("Demo source identity is occupied by another source")
    return False


async def get_source(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> Source | None:
    """Return fresh owner-scoped Source ORM for Source routes; never authorize by UUID alone."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(
        select(Source)
        .where(
            Source.id == source_id,
            *_source_scope(scope),
        )
        .execution_options(populate_existing=True)
    )


CHAT_ATTACHMENTS_MARKER = "chat_attachments"
CHAT_ATTACHMENTS_NAME = "Chat attachments"
CHAT_ATTACHMENTS_SHARED_NAME = "Chat attachments (shared)"


async def _get_or_create_marked_manual_source(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    marker: str,
    name: str,
    local_only: bool,
    lock_key: str,
) -> Source:
    """Return the workspace's non-archived manual source carrying ``marker``/``local_only``, creating it once.

    Access locks come first, then a transaction-scoped advisory lock on ``lock_key`` serializes
    concurrent first uploads. An archived/purged source is retired history and a fresh one is
    created; a paused one is returned so intake reports it as inactive. Commits before returning.
    """
    access_fence = await _admit_source_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=True,
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    source = await session.scalar(
        select(Source)
        .where(
            *_source_scope(scope),
            Source.configuration[marker].as_boolean().is_(True),
            Source.local_only.is_(local_only),
            Source.status != "archived",
        )
        .order_by(Source.created_at.desc())
        .limit(1)
    )
    if source is not None:
        await session.commit()
        return source
    # Built directly: create_source forces local_only for manual sources, which would break "shared".
    source = Source(
        workspace_id=scope.workspace_id,
        type="manual",
        name=name,
        local_only=local_only,
        configuration={marker: True},
    )
    session.add(source)
    await session.flush()
    await commit_with_replay(
        session,
        [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def get_or_create_chat_attachments_source(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
    shared: bool = False,
) -> Source:
    """Return one of the workspace's two server-owned Chat attachments manual sources, creating it once.

    ``shared=False`` (the default) selects the private "Chat attachments" source (``local_only``);
    ``shared=True`` selects "Chat attachments (shared)", which is not ``local_only`` and therefore
    eligible for cloud chat, embeddings, extraction and briefs. Both carry the same server-set
    marker and differ only in ``local_only``, which no owner API can change. Commits before returning.
    """
    variant = "shared" if shared else "private"
    return await _get_or_create_marked_manual_source(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        marker=CHAT_ATTACHMENTS_MARKER,
        name=CHAT_ATTACHMENTS_SHARED_NAME if shared else CHAT_ATTACHMENTS_NAME,
        local_only=not shared,
        lock_key=f"umwelt.sources.chat_attachments.{scope.workspace_id}.{variant}",
    )


DOCUMENT_UPLOADS_MARKER = "document_uploads"
DOCUMENT_UPLOADS_NAME = "Uploads"


async def get_or_create_uploads_source(
    session: AsyncSession,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> Source:
    """Return the workspace's server-owned default "Uploads" manual source, creating it once.

    Used by Documents uploads that name no source. The source is ``local_only`` (manual default),
    which also satisfies the notes/finance import rule that the target be a local manual source.
    Concurrency, archived-source and paused-source handling match the Chat attachments source.
    Commits before returning.
    """
    return await _get_or_create_marked_manual_source(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        marker=DOCUMENT_UPLOADS_MARKER,
        name=DOCUMENT_UPLOADS_NAME,
        local_only=True,
        lock_key=f"umwelt.sources.document_uploads.{scope.workspace_id}",
    )


async def is_chat_attachments_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> bool:
    """Report whether an in-workspace source carries the server-set Chat attachments marker."""
    source = await get_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return source is not None and source.configuration.get(CHAT_ATTACHMENTS_MARKER) is True


async def get_tool_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    source_ids: frozenset[UUID],
    owner_all: bool = False,
    destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourceRead | None:
    """Read an active source projection within exact scope and destination privacy policy."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not owner_all and source_id not in source_ids:
        return None
    row = (
        await session.execute(
            select(
                Source.id,
                Source.name,
                Source.type,
                Source.status,
                Source.generation,
                Source.local_only,
                Source.created_at,
            ).where(Source.id == source_id, Source.status == "active", *_source_scope(scope))
        )
    ).one_or_none()
    if row is not None and destination != ToolDestination.LOCAL and row.local_only:
        return None
    return ToolSourceRead(*row) if row else None


async def list_tool_sources(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    limit: int,
    cursor: str | None,
    source_ids: frozenset[UUID],
    owner_all: bool = False,
    destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourcePage:
    """Page active, exact-scope sources after destination privacy filtering.

    Remote local-only rows are excluded before ordering, limit and cursor construction; the
    projection contains no source configuration or credentials.
    """
    access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    fingerprint = _cursor_fingerprint(
        access,
        scope,
        "tool-sources",
        "created_at,id:desc",
        limit,
        sorted(str(identifier) for identifier in source_ids),
        owner_all,
        destination.value,
    )
    position = _decode_source_page_cursor(cursor, fingerprint) if cursor is not None else None
    if not 1 <= limit <= 100:
        raise ValueError("Source tool page size is outside its supported bound")
    statement = select(
        Source.id,
        Source.name,
        Source.type,
        Source.status,
        Source.generation,
        Source.local_only,
        Source.created_at,
    ).where(Source.status == "active", *_source_scope(scope))
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if not owner_all:
        if not source_ids:
            return ToolSourcePage((), None)
        statement = statement.where(Source.id.in_(source_ids))
    if position is not None:
        statement = statement.where(tuple_(Source.created_at, Source.id) < position)
    rows = list(
        (
            await session.execute(
                statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    page = rows[:limit]
    next_cursor = (
        _encode_source_page_cursor(page[-1].created_at, page[-1].id, fingerprint)
        if more and page
        else None
    )
    return ToolSourcePage(tuple(ToolSourceRead(*row) for row in page), next_cursor)
