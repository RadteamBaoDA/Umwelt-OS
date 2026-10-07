import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Integer, Select, and_, case, cast, desc, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.events import DomainEvent
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_source_change
from core.tools.schemas import ToolDestination
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import (
    ConnectorSource,
    GadgetSourceSelection,
    GadgetSourceSelectionPage,
    OperationRead,
    SourceCreate,
    SourceExportFence,
    SourceFence,
    SourceFenceSet,
    SourceMetadataExportFence,
    SourceMetadataExportPage,
    SourceMetadataExportValidation,
    SourcePatch,
    SourceRead,
)


def _require_source_owner(scope: Scope) -> None:
    """Reject member metadata access; detached scope construction still requires admission."""
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit Source scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


def _source_scope(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Bind SQL to an admitted owner workspace and any exact internal source/generation."""
    _require_source_owner(scope)
    predicates: tuple[ColumnElement[bool], ...] = (Source.workspace_id == scope.workspace_id,)
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates += (Source.id == scope.source_id, Source.generation == scope.source_generation)
    return predicates


def _operation_scope(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Bind retained receipt queries without requiring a still-existing canonical Source."""
    _require_source_owner(scope)
    actor_user_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    predicates: tuple[ColumnElement[bool], ...] = (
        SourcePurgeOperation.workspace_id == scope.workspace_id,
        SourcePurgeOperation.actor_user_id == actor_user_id,
    )
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates += (
            SourcePurgeOperation.source_id == scope.source_id,
            SourcePurgeOperation.generation == scope.source_generation,
        )
    return predicates


async def _admit_source_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Check real owner admission; lock auth/workspace/membership before any domain locks.

    The configured instance flag is explicit. No commit, network or fabricated session proof;
    deferred publication callers supply the previously captured fence for configuration CAS.
    A caller already holding domain locks must reuse its admission, never call this with lock.
    """
    _require_source_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _cursor_fingerprint(fence: AccessFence, scope: Scope, *filters: object) -> str:
    """Fingerprint actual actor/workspace/revisions plus exact selector and ordering inputs."""
    values = [str(fence.workspace_id), fence.user_id, fence.membership_revision,
              fence.configuration_revision, *filters]
    if isinstance(scope, InternalJobScope):
        values += [str(scope.source_id) if scope.source_id else None, scope.source_generation]
    return hashlib.sha256(json.dumps(values, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _encode_source_page_cursor(created_at: datetime, identifier: UUID, fingerprint: str) -> str:
    """Wrap the existing keyset primitive with the exact admitted request fingerprint."""
    raw = json.dumps([2, fingerprint, encode_cursor(created_at, identifier)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_source_page_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Reject legacy, foreign-principal, changed-filter and changed-revision continuations."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 3 or values[:2] != [2, fingerprint]:
            raise ValueError
        created_at, identifier = decode_cursor(values[2])
        if _encode_source_page_cursor(created_at, identifier, fingerprint) != cursor:
            raise ValueError
        return created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Source cursor is invalid") from exc


def _encode_source_export_cursor(
    snapshot_at: datetime, created_at: datetime, identifier: UUID, fingerprint: str,
) -> str:
    """Bind a canonical source metadata keyset position to one fixed owner snapshot."""
    snapshot_fingerprint = hashlib.sha256(f"{fingerprint}:{snapshot_at.isoformat()}".encode()).hexdigest()
    payload = json.dumps(
        [2, "sources", snapshot_fingerprint, snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_source_export_cursor(cursor: str, fingerprint: str) -> tuple[datetime, datetime, UUID]:
    """Reject oversized, noncanonical, cross-dataset, or future source export cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 6 or values[:2] != [2, "sources"]:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(values[3]), datetime.fromisoformat(values[4])
        identifier = UUID(values[5])
        snapshot_fingerprint = hashlib.sha256(f"{fingerprint}:{snapshot_at.isoformat()}".encode()).hexdigest()
        if (any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, created_at))
                or values[2] != snapshot_fingerprint
                or snapshot_at.isoformat() != values[3] or created_at.isoformat() != values[4]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != values[5]
                or _encode_source_export_cursor(snapshot_at, created_at, identifier, fingerprint) != cursor):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Source export cursor is invalid") from exc


def _source_export_scope(snapshot_at: datetime, *, scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Filter an already-admitted owner's workspace before snapshot counts and page limits."""
    return (
        *_source_scope(scope),
        Source.created_at <= snapshot_at,
        Source.updated_at <= snapshot_at,
        Source.id.in_(export_eligible_source_ids(scope=scope)),
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
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
) -> SourceMetadataExportPage:
    """Return a bounded source metadata export, excluding all provider configuration and secrets."""
    fence = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != fence.user_id or record_kind != "sources" or not 1 <= limit <= 100:
        raise ValueError("Source export owner, kind or page limit is invalid")
    fingerprint = _cursor_fingerprint(fence, scope, "sources-export", "created_at,id:asc", record_kind, limit)
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_source_export_cursor(cursor, fingerprint)
        position = (position_at, position_id)
    snapshot_count = int(await session.scalar(
        select(func.count()).select_from(Source).where(*_source_export_scope(snapshot_at, scope=scope))
    ) or 0)
    statement = select(*_source_export_columns()).where(*_source_export_scope(snapshot_at, scope=scope))
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
        source_id=row.id, workspace_id=scope.workspace_id, created_at=row.created_at, updated_at=row.updated_at,
        generation=row.generation, content_digest=hashlib.sha256(raw).hexdigest(),
    ) for row, raw in zip(rows, encoded_items, strict=True)]
    return SourceMetadataExportPage(
        owner_id=owner_id, workspace_id=scope.workspace_id, record_kind="sources", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_source_export_cursor(snapshot_at, rows[-1].created_at, rows[-1].id, fingerprint)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[SourceMetadataExportFence],
) -> SourceMetadataExportValidation:
    """Recheck source eligibility, generation, row content, and fixed-cutoff inventory."""
    access = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if owner_id != access.user_id or record_kind != "sources" or len(fences) > 100:
        raise ValueError("Source export validation input is invalid")
    if any(fence.workspace_id != scope.workspace_id for fence in fences):
        return SourceMetadataExportValidation(valid=False, reason="record_changed", observed_snapshot_count=0)
    observed = int(await session.scalar(
        select(func.count()).select_from(Source).where(*_source_export_scope(snapshot_at, scope=scope))
    ) or 0)
    if observed != expected_snapshot_count:
        return SourceMetadataExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    source_fences = [SourceExportFence(
        source_id=item.source_id, workspace_id=item.workspace_id, generation=item.generation,
    ) for item in fences]
    if len(await filter_export_eligible_sources(
        session, source_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )) != len(source_fences):
        return SourceMetadataExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = (await session.execute(
            select(*_source_export_columns()).where(
                Source.id == fence.source_id, *_source_export_scope(snapshot_at, scope=scope),
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


def export_eligible_source_ids(*, scope: Scope) -> Select[UUID]:
    """Return a SQL source-ID projection excluding committed, unfinished data purges.

    The predicate is deliberately source-owned so export consumers can filter and count
    eligible rows before paging without importing SourcePurgeOperation or materializing
    the source table. Archived connector-only sources remain eligible because they have
    no data-purge operation. Caller admits the supplied owner scope before executing SQL;
    this pure projection never replaces current account/membership authorization.
    """
    pending_data_purges = select(SourcePurgeOperation.source_id).where(
        SourcePurgeOperation.workspace_id == scope.workspace_id,
        SourcePurgeOperation.status.in_(("queued", "running", "failed"))
    )
    return select(Source.id).where(*_source_scope(scope), ~Source.id.in_(pending_data_purges))


async def filter_export_eligible_sources(
    session: AsyncSession, fences: Sequence[SourceExportFence], *, scope: Scope, multi_workspace_enabled: bool
) -> tuple[UUID, ...]:
    """Keep a bounded set of captured source generations with no unfinished data purge."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(fences) > 100:
        raise ValueError("Source export fence set exceeds its page limit")
    source_ids = [fence.source_id for fence in fences]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("Source export fences must have unique source IDs")
    if any(fence.workspace_id != scope.workspace_id for fence in fences):
        return ()
    if not source_ids:
        return ()
    generations = {fence.source_id: fence.generation for fence in fences}
    rows = (await session.execute(
        select(Source.id, Source.generation)
        .where(Source.id.in_(source_ids), Source.id.in_(export_eligible_source_ids(scope=scope)))
    )).all()
    return tuple(
        source_id for source_id, generation in rows
        if generations.get(source_id) == generation
    )


async def observability_quality_summary(
    session: AsyncSession, *, instance_operator: bool, now: datetime | None = None,
) -> dict[str, int]:
    """Aggregate instance health only for an already-admitted bootstrap operator/internal caller.

    The capability flag is internal, never supplied by an HTTP body or workspace owner;
    O owns operator admission before calling this deliberate global, content-free projection.
    """
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
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


async def create_source(session: AsyncSession, payload: SourceCreate, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source:
    """Create in the owner workspace after access locks, preserving wrapper replay/commit.

    No network I/O occurs; an optional captured fence rejects stale deferred configuration.
    The locked access fence is handed to replay, which must not acquire earlier locks again.
    """
    access_fence = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True, expected=expected_access_fence)
    source = Source(
        workspace_id=scope.workspace_id,
        type=payload.type,
        name=payload.name,
        provider=payload.provider,
        local_only=payload.type == "manual",
    )
    session.add(source)
    await session.flush()
    await commit_with_replay(
        session, [make_source_change(source.id, source.generation, source.status, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def ensure_demo_source(session: AsyncSession, source_id: UUID, namespace: str, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Stage a verified demo owner's Source once under access locks, rejecting foreign IDs.

    No commit or default-owner fallback; namespace and workspace must both match on collision.
    Caller proves demo-owner lineage and supplies its configured flag/current revision.
    An existing Source must match the bound current generation, including generations above 1;
    only a missing Source may be created at generation 1, never resurrected at a later epoch.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True, expected=expected_access_fence)
    if isinstance(scope, InternalJobScope) and scope.source_id not in (None, source_id):
        raise ValueError("Demo Source identity does not match its scope")
    source = await session.scalar(select(Source).where(
        Source.id == source_id, *_source_scope(scope),
    ).execution_options(populate_existing=True))
    if source is not None:
        if source.configuration.get("demo_namespace") != namespace:
            raise RuntimeError("Demo source identity is occupied by another source")
        return False
    if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
        scope.source_generation != 1
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
    source = await session.scalar(select(Source).where(
        Source.id == source_id, *_source_scope(scope),
    ).execution_options(populate_existing=True))
    if source is None or source.configuration.get("demo_namespace") != namespace:
        raise RuntimeError("Demo source identity is occupied by another source")
    return False


async def get_source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> Source | None:
    """Return fresh owner-scoped Source ORM for Source routes; never authorize by UUID alone."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(select(Source).where(
        Source.id == source_id, *_source_scope(scope),
    ).execution_options(populate_existing=True))


async def get_tool_source(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool, source_ids: frozenset[UUID],
    owner_all: bool = False, destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourceRead | None:
    """Read an active source projection within exact scope and destination privacy policy."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not owner_all and source_id not in source_ids:
        return None
    row = (await session.execute(
        select(Source.id, Source.name, Source.type, Source.status, Source.generation,
               Source.local_only, Source.created_at)
        .where(Source.id == source_id, Source.status == "active", *_source_scope(scope))
    )).one_or_none()
    if row is not None and destination != ToolDestination.LOCAL and row.local_only:
        return None
    return ToolSourceRead(*row) if row else None


async def list_tool_sources(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, limit: int, cursor: str | None,
    source_ids: frozenset[UUID], owner_all: bool = False,
    destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolSourcePage:
    """Page active, exact-scope sources after destination privacy filtering.

    Remote local-only rows are excluded before ordering, limit and cursor construction; the
    projection contains no source configuration or credentials.
    """
    access = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    fingerprint = _cursor_fingerprint(
        access, scope, "tool-sources", "created_at,id:desc", limit,
        sorted(str(identifier) for identifier in source_ids), owner_all, destination.value,
    )
    position = _decode_source_page_cursor(cursor, fingerprint) if cursor is not None else None
    if not 1 <= limit <= 100:
        raise ValueError("Source tool page size is outside its supported bound")
    statement = select(
        Source.id, Source.name, Source.type, Source.status, Source.generation,
        Source.local_only, Source.created_at,
    ).where(Source.status == "active", *_source_scope(scope))
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if not owner_all:
        if not source_ids:
            return ToolSourcePage((), None)
        statement = statement.where(Source.id.in_(source_ids))
    if position is not None:
        statement = statement.where(tuple_(Source.created_at, Source.id) < position)
    rows = list((await session.execute(
        statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1)
    )).all())
    more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_source_page_cursor(page[-1].created_at, page[-1].id, fingerprint) if more and page else None
    return ToolSourcePage(tuple(ToolSourceRead(*row) for row in page), next_cursor)


def _connector_source(source: Source) -> ConnectorSource:
    """Build a defensive public connector projection from a source record."""
    return ConnectorSource(
        id=source.id,
        workspace_id=source.workspace_id,
        type=source.type,
        status=source.status,
        generation=source.generation,
        configuration=deepcopy(source.configuration or {}),
        provider=source.provider,
    )


async def get_connector_source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> ConnectorSource | None:
    """Read fresh retained source configuration and project it for connector consumers.

    populate_existing keeps generation, status and provider scope checks current even when the
    caller's identity map already holds this source; this remains a read with no row lock.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await session.scalar(select(Source).where(Source.id == source_id, *_source_scope(scope))
                                  .execution_options(populate_existing=True))
    return _connector_source(source) if source is not None else None


async def _lock_source_row(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> tuple[Source | None, AccessFence]:
    """Return the refreshed Source and access fence under auth->workspace->Source locks.

    Entry requires no previously held domain locks. Optional expected fence performs deferred
    configuration CAS before Source locking; caller owns commit and release before external I/O.
    """
    access_fence = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True, expected=expected_access_fence)
    source = await session.scalar(
        select(Source)
        .where(Source.id == source_id, *_source_scope(scope))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return source, access_fence


async def get_source_fence(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> SourceFence | None:
    """Read a detached source eligibility fence without acquiring a database row lock.

    Owner: modules/sources
    Fields: id, workspace_id, status, generation, local_only.
    Current owner admission and any internal exact generation are required; absent/invisible
    Source returns None. It is read-only and grants no authority to fetch provider items.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await session.scalar(select(Source).where(
        Source.id == source_id, *_source_scope(scope),
    ).execution_options(populate_existing=True))
    if source is None:
        return None
    return SourceFence(
        id=source.id, workspace_id=source.workspace_id, status=source.status, generation=source.generation, local_only=source.local_only
    )


async def github_retirement_peer_projection_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> Select[tuple[UUID]]:
    """Return sibling IDs solely for exact archived GitHub provider-revoke safety.

    Internal retirement caller holds ordered account/workspace/membership, anchor Source,
    provisioning and relevant grant locks in this transaction. Fresh real owner/default-
    workspace admission and complete access/Source fences must match the unchanged scope,
    including any bound source generation; member, absent, stale, foreign, non-GitHub or
    nonarchived anchors are denied. Canonical deletion cannot authorize this live-anchor
    query from a bare UUID. No locks, mutations, commit or provider I/O occur here.

    The purpose-bound sibling relation selects only one column named id in the verified
    anchor workspace, excluding the anchor and archived peers. It exposes no rows, ORM,
    configuration, content or credentials and does not replace/strip the bound principal.
    These IDs grant no peer read/write authority or general Source enumeration. Connector
    joins its token-bearing same-remote-user grants before ordering and LIMIT101; this
    projection has no provider filter, order or limit, preserving conservative peer safety.
    Execute only in this held transaction: the retained workspace lock serializes lifecycle
    and grant publication by converted writers, so no later peer Source locks are acquired.
    """
    anchor = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if anchor is None or anchor.status != "archived" or anchor.provider != "github":
        raise HTTPException(status_code=409, detail="GitHub retirement anchor is unavailable")
    return select(Source.id).where(
        Source.workspace_id == anchor.workspace_id,
        Source.id != anchor.id,
        Source.status != "archived",
    )


def ingestion_lifecycle_projection(*, scope: Scope) -> Select[UUID, str, int]:
    """Return the minimal source lifecycle read projection for ingestion retry aggregation.

    Cross-module callers may correlate ingestion-owned retry records against the
    current source identity, status, and generation. Configuration and content
    remain private. Caller admits owner scope before execution; this projection is already
    workspace/source-generation filtered and grants no write authority.
    """
    return select(Source.id, Source.status, Source.generation).where(*_source_scope(scope))


def ingestion_instance_lifecycle_projection(*, instance_operator: bool) -> Select[UUID, str, int]:
    """Expose only instance Source lifecycle columns to an already-admitted bootstrap operator.

    Internal ingestion observability callers prove real operator authority before this pure
    projection. Never derive the capability from an HTTP body or workspace owner role; no
    metadata/configuration/credentials/content or implicit workspace grant is returned.
    """
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    return select(Source.id, Source.status, Source.generation)


async def lock_source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourceFence | None:
    """Lock access before one Source and return its scoped lifecycle; missing returns None.

    Called before any domain locks; use lock_source_set for multiple Sources in a transaction.
    """
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    return SourceFence(
        id=source.id, workspace_id=source.workspace_id, status=source.status, generation=source.generation, local_only=source.local_only
    )


async def lock_retained_evidence_source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourceFence | None:
    """Share-lock a source while confirming retained data remains eligible for owner reads/effects.

    Paused and connector-only archived sources remain eligible. A queued, running, or failed
    with-data purge revokes eligibility immediately, even while immutable document rows linger.
    The PostgreSQL ``FOR SHARE`` lock is compatible with other evidence readers but conflicts with
    Source lifecycle ``FOR UPDATE`` mutations. Use lock_source_set(retained_evidence=True)
    for multiple Sources before Documents/domain locks; hold until publication commit.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True, expected=expected_access_fence)
    fences = await _lock_source_fence_rows(session, (source_id,), scope=scope, retained_evidence=True)
    return fences[0] if fences is not None else None


async def _lock_source_fence_rows(
    session: AsyncSession, source_ids: tuple[UUID, ...], *, scope: Scope, retained_evidence: bool,
) -> tuple[SourceFence, ...] | None:
    """Lock an admitted sorted Source set and return nothing unless the entire set matches.

    Caller already holds current auth/workspace/membership locks. This helper takes no earlier
    locks, grants no admission, performs no commit, and constructs DTOs only after completeness
    and retained purge predicates hold. Single and batch retained reads use the same logic.
    """
    if not source_ids:
        return ()
    rows = list(await session.scalars(select(Source).where(
        Source.id.in_(source_ids), *_source_scope(scope),
    ).order_by(Source.id).with_for_update(read=retained_evidence)
        .execution_options(populate_existing=True)))
    if len(rows) != len(source_ids):
        return None
    if retained_evidence:
        eligible_ids = set(await session.scalars(export_eligible_source_ids(scope=scope).where(
            Source.id.in_(source_ids),
        )))
        if eligible_ids != set(source_ids):
            return None
    return tuple(SourceFence(
        id=source.id, workspace_id=source.workspace_id, status=source.status,
        generation=source.generation, local_only=source.local_only,
    ) for source in rows)


async def lock_source_set(
    session: AsyncSession, source_ids: Sequence[UUID], *, scope: Scope, multi_workspace_enabled: bool,
    retained_evidence: bool = False, expected_access_fence: AccessFence | None = None,
) -> SourceFenceSet:
    """Admit once, then lock a complete deduplicated Source set in UUID order, bounded to 500.

    Ordinary mode preserves lifecycle snapshots; retained mode uses share locks and denies
    queued/running/failed data purges while allowing pause/connector-only archive. Any missing,
    foreign or exact-generation mismatch fails the whole set with 404, without partial DTOs.
    Caller holds no prior domain locks and owns transaction release, publication and commit;
    pass the captured fence for deferred CAS and use the returned fence for scoped replay.
    """
    if len(source_ids) > 500 or any(not isinstance(identifier, UUID) for identifier in source_ids):
        raise ValueError("Source lock set requires at most 500 UUIDs")
    if type(retained_evidence) is not bool:
        raise TypeError("Source lock mode must be a boolean")
    ordered = tuple(sorted(set(source_ids), key=str))
    access_fence = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        lock=True, expected=expected_access_fence,
    )
    fences = await _lock_source_fence_rows(session, ordered, scope=scope, retained_evidence=retained_evidence)
    if fences is None:
        raise HTTPException(status_code=404, detail="Source set is unavailable")
    return SourceFenceSet(fences=fences, access_fence=access_fence)


async def filter_active_source_ids(session: AsyncSession, source_ids: Sequence[UUID], *, scope: Scope, multi_workspace_enabled: bool
) -> tuple[UUID, ...]:
    """Return only owner sources that are active and still eligible for current reads."""
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not source_ids or len(source_ids) > 32:
        return ()
    rows = await session.scalars(
        select(Source.id).where(
            Source.id.in_(source_ids), Source.status == "active", Source.retired_at.is_(None),
            *_source_scope(scope), Source.id.in_(export_eligible_source_ids(scope=scope)),
        )
    )
    return tuple(rows.all())



async def lock_source_for_document(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> None:
    """Validate the source and hold its lock until the caller's transaction ends."""
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        raise LookupError("Source not found")
    if source.status != "active":
        raise ValueError("Cannot add documents to an inactive source")


async def set_connector_configuration(
    session: AsyncSession,
    source_id: UUID,
    expected_generation: int,
    configuration: dict[str, object],
    *, scope: Scope, multi_workspace_enabled: bool,
    allow_paused: bool = False,
    expected_access_fence: AccessFence | None = None,
) -> ConnectorSource | None:
    """Replace only admitted workspace configuration under matching generation/lifecycle locks.

    Optional captured access fence is rechecked before Source; deepcopy and flush only, no commit.
    """
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None or source.status == "archived" or source.generation != expected_generation:
        return None
    if source.status != "active" and not (allow_paused and source.status == "paused"):
        return None
    source.generation += 1
    source.configuration = deepcopy(configuration)
    await session.flush()
    return _connector_source(source)


async def record_collection_started(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush collection-start timestamps after ordered access/Source and exact-generation checks.

    No external work or commit; caller releases these locks before fetching provider content.
    """
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_collection_started(session, source, expected_generation, at)


async def _record_collection_started(
    session: AsyncSession, source: Source | None, expected_generation: int, at: datetime,
) -> bool:
    """Flush start timestamps for the current active exact Source generation; acquire no locks.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
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
    *, scope: Scope, multi_workspace_enabled: bool,
    no_changes: bool = False,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush collection outcome only under current access and active exact-generation locks.

    Deferred provider publishers pass their captured access fence for configuration CAS, in a
    fresh transaction after network work. This helper introduces no network I/O or commit.
    """
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_collection_result(session, source, expected_generation, at, error_code, no_changes=no_changes)


async def _record_collection_result(
    session: AsyncSession, source: Source | None, expected_generation: int, at: datetime, error_code: str | None, *, no_changes: bool = False,
) -> bool:
    """Flush the unchanged success/error/no-change health rules for an active exact generation.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
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
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, error_code: str | None, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> bool:
    """Flush processing outcome under ordered access/Source and active generation fences.

    Deferred publishers pass captured access fence; no commit or model/provider I/O occurs here.
    """
    source, _access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    return await _record_processing_result(session, source, expected_generation, at, error_code)


async def _record_processing_result(
    session: AsyncSession, source: Source | None, expected_generation: int, at: datetime, error_code: str | None,
) -> bool:
    """Flush existing processing success/error health fields for an active exact generation.

    Caller validates/holds ordered access and Source locks. This owner-private mutation
    is shared by acquiring and transaction-local entrypoints; no commit or remote I/O.
    """
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


async def _source_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> Source | None:
    """Revalidate fresh scope and exact Source under the caller's existing ordered locks.

    Caller acquired auth/workspace/membership then Source before run/state/domain locks and
    retains that transaction. Detached values are required snapshots, never lock proof; this
    internal owner path cannot be exposed as HTTP authority. Current access disagreement409
    and unavailable/mismatching Source returns None. No locks, commit or remote I/O acquired.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise TypeError("Captured access and Source fences are required")
    current_access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence:
        raise HTTPException(status_code=409, detail="Workspace access fence changed")
    if source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id:
        return None
    source = await session.scalar(select(Source).where(
        Source.id == source_id, *_source_scope(scope),
    ).execution_options(populate_existing=True))
    if source is None or (
        source.generation != source_fence.generation or source.status != source_fence.status
        or source.local_only != source_fence.local_only
    ):
        return None
    return source


async def record_collection_started_in_uow(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Flush a collection start using exact caller-held access/Source snapshots, without locking.

    Internal composition only: caller retains ordered admission/Source locks before run/state
    locks. Fresh access is revalidated; inactive/missing/stale Source returns False. Same health
    behavior as the acquiring wrapper, no commit or network and no authority from DTO creation.
    """
    source = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    return await _record_collection_started(session, source, expected_generation, at)


async def record_collection_result_in_uow(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, error_code: str | None, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
    no_changes: bool = False,
) -> bool:
    """Flush existing success/error/no-change rules under fresh caller-held Source/access proof.

    Caller retains ordered admission/Source locks before run/state/domain locks; DTOs cannot
    create that authority and this seam is never HTTP admission. Missing/inactive/stale Source
    returns False; fresh access mismatch409. No earlier lock, commit or provider I/O occurs.
    """
    source = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    return await _record_collection_result(session, source, expected_generation, at, error_code, no_changes=no_changes)


async def record_processing_result_in_uow(
    session: AsyncSession, source_id: UUID, expected_generation: int, at: datetime, error_code: str | None, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Flush processing success/error through fresh proof of the caller's locked exact Source.

    Internal composition requires actual ordered admission/Source locks before downstream
    domain locks, retained until commit. Missing/inactive/stale returns False; current access
    mismatch409. This only revalidates nonlocking snapshots and flushes existing health rules.
    """
    source = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    return await _record_processing_result(session, source, expected_generation, at, error_code)


async def list_sources(
    session: AsyncSession, limit: int, cursor: str | None, *, scope: Scope, multi_workspace_enabled: bool
) -> tuple[list[Source], str | None]:
    """Return an owner-scoped bounded descending page; reject foreign/unbound cursors."""
    access = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Source page limit must be between 1 and 100")
    fingerprint = _cursor_fingerprint(access, scope, "sources", "created_at,id:desc", limit)
    statement = select(Source).where(*_source_scope(scope)).order_by(desc(Source.created_at), desc(Source.id))
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint) if has_more and rows else None
    return rows, next_cursor


async def get_gadget_sources(
    session: AsyncSession, source_ids: tuple[UUID, ...], *, scope: Scope, multi_workspace_enabled: bool
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
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, limit: int = 32, cursor: str | None = None,
) -> GadgetSourceSelectionPage:
    """Page detached active-source identities for owner News selection and bounded catch-up.

    This contract contains only the existing gadget-safe source fields, applies
    active lifecycle and stable creation/ID keyset filters, and excludes scope,
    configuration, credentials and provider item policy. Callers must still use
    Documents current-version/scope fences for each item; this is not item-level
    authorization and does not grant agent access.
    """
    access = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    fingerprint = _cursor_fingerprint(access, scope, "active-gadget-sources", "created_at,id:desc", limit)
    if not 1 <= limit <= 32:
        raise ValueError("Active source projection limit must be between 1 and 32")
    statement = select(
        Source.id, Source.name, Source.type, Source.provider, Source.status,
        Source.generation, Source.local_only, Source.created_at,
    ).where(Source.status == "active", *_source_scope(scope))
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(statement.order_by(desc(Source.created_at), desc(Source.id)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    items = tuple(GadgetSourceSelection(
        id=row.id, name=row.name, type=row.type, provider=row.provider,
        status=row.status, generation=row.generation, local_only=row.local_only,
    ) for row in rows)
    next_cursor = _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint) if more and rows else None
    return GadgetSourceSelectionPage(items=items, next_cursor=next_cursor)


async def list_gadget_sources(
    session: AsyncSession, limit: int = 50, cursor: str | None = None, *, scope: Scope, multi_workspace_enabled: bool
) -> GadgetSourceSelectionPage:
    """Return an authorized caller's bounded source-selection page using created-at/ID keyset order.

    Only dashboard selection fields are queried, so connector configuration,
    credentials, scopes, and source content never enter this detached page.
    Current workspace owner admission is required before paging; this projection
    does not establish provider item-level authorization.
    """
    access = await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    fingerprint = _cursor_fingerprint(access, scope, "gadget-sources", "created_at,id:desc", limit)
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
    ).where(*_source_scope(scope)).order_by(desc(Source.created_at), desc(Source.id))
    if cursor is not None:
        timestamp, identifier = _decode_source_page_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Source.created_at, Source.id) < (timestamp, identifier))
    rows = list((await session.execute(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_source_page_cursor(rows[-1].created_at, rows[-1].id, fingerprint) if has_more and rows else None
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
    session: AsyncSession, source: Source, payload: SourcePatch, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source | None:
    """Apply an admitted Source patch with ordered inactive cleanup or active hint rearm.

    Pausing or archiving fences collection before pending hints are terminalized in this same
    transaction. Explicit reactivation rearms only hints whose current grant and provisioning
    binding match the new source generation; it never resumes a stale OAuth binding.
    Requery the supplied ORM identity in the owner's workspace after access CAS locks; retain
    this wrapper's scoped replay/commit and hand replay the captured locked access fence.
    """
    locked_source, access_fence = await _lock_source_row(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
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
                await _fence_connector_source(
                    session, source, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence,
                )
            if source.provider == "github" and next_status == "active":
                await _reconcile_github_hint_lifecycle(
                    session, source, active=next_status == "active", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
    drafts = [make_source_change(source.id, source.generation, source.status, scope=scope)] if changed else []
    await commit_with_replay(
        session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def pause_source_for_connector(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> ConnectorSource | None:
    """Pause an admitted exact-generation Source and run ordered cleanup in caller transaction.

    Access CAS locks precede Source; one verified generation transition is forwarded to scoped
    cleanup callees. Flush only, no commit or provider I/O; caller releases before external work.
    """
    source, access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None or source.status == "archived":
        return None
    if source.status != "paused":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(
        session, source, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.flush()
    return _connector_source(source)


async def prepare_source_pause_for_connector_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare exact Source pause cleanup rows without mutating or granting new authority.

    Internal acceptance caller already holds ordered account/workspace/Source/provisioning
    locks and calls before GitHub grant/state/run locks. Fresh active or paused G/access must
    match the complete captured fences; unavailable Source raises409. Both active G->paused
    G+1 and identical paused G cleanup prepare Connector slots/browser grant, Ingestion token
    rows, then Tools jobs/evidence in their owner-defined sorted order. No Source transition,
    revocation, receipt, hint/capacity change, commit or network occurs here. Preparation
    returns no token; caller retains this transaction through exact proof and late apply.
    """
    source = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused"}:
        raise HTTPException(status_code=409, detail="Source pause preparation is unavailable")
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import lock_source_browser_results_in_uow

    await connectors.prepare_source_pause_cleanup_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await ingestion.lock_source_credentials_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await lock_source_browser_results_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def pause_source_for_connector_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> ConnectorSource | None:
    """Apply prepared active G->paused G+1 or exact paused G cleanup with the same principal.

    Internal caller retains actual ordered Source/provisioning/cleanup locks plus the complete
    hint set before capacity; validated acceptance precedes apply. Fresh original G/access
    proof is checked before mutation; missing, archived or mismatching Source returns None.
    Active Source transitions once; already-paused Source retains generation, retired_at and
    the exact supplied cleanup scope. Only active-transition cleanup callees receive the
    narrowly transitioned scope; no collector readmission or successor principal escapes.
    Both cases repeat complete credential/browser/provisioning/hint effects using prepared-row
    owner mutations, never an acquiring lifecycle helper. Singular Source browser purge takes
    the original AccessFence and actual paused current fence with only that authorized scope
    transition; historical jobs gain no successor authority. Flush only; caller skips inactive
    health recording and commits atomically, with G+1 replay only for the actual transition.
    """
    source = await _source_in_uow(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused"}:
        return None
    cleanup_scope = scope
    if source.status == "active":
        source.generation += 1
        source.status = "paused"
        source.retired_at = datetime.now(UTC)
        cleanup_scope = _transitioned_source_scope(scope, source)
    paused_fence = SourceFence(
        id=source.id, workspace_id=source.workspace_id, status=source.status,
        generation=source.generation, local_only=source.local_only,
    )
    # Owner mutation hooks freshly read the paused row; persist it within the held transaction.
    await session.flush()
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import purge_source_browser_results_in_uow

    await ingestion.revoke_source_credentials(
        session, source.id, scope=cleanup_scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await purge_source_browser_results_in_uow(
        session, source.id, scope=cleanup_scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=paused_fence,
    )
    await connectors.apply_source_pause_cleanup_in_uow(
        session, source_fence=paused_fence, previous_source_fence=source_fence,
        scope=cleanup_scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    await session.flush()
    return _connector_source(source)


async def archive_source(
    session: AsyncSession, source_id: UUID, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> Source | None:
    """Archive in the owner workspace under ordered access CAS/Source locks and scoped replay.

    Connector/token/browser preparation precedes mutations, retirement and full hint release;
    the captured access fence is reused by replay without taking earlier locks again.
    """
    source, access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    changed = source.status != "archived"
    if source.status != "archived":
        source.generation += 1
        source.status = "archived"
        source.retired_at = datetime.now(UTC)
    await _fence_connector_source(
        session, source, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    drafts = [make_source_change(source.id, source.generation, source.status, scope=scope)] if changed else []
    await commit_with_replay(
        session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    await session.refresh(source)
    return source


async def start_source_purge(
    session: AsyncSession, source_id: UUID, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
    expected_access_fence: AccessFence | None = None,
) -> SourcePurgeOperation | None:
    """Queue Source purge after ordered lifecycle cleanup, then own outbox/replay commit.

    New operations carry no raw URI snapshot: Documents creates durable per-document cleanup
    receipts under its publication identity locks before the canonical cascade. The legacy
    JSON column remains readable only for explicit compatibility repair.
    Capture workspace, actual actor and membership revision on the retained operation and
    event atomically; preserve this wrapper's scoped replay commit without provider I/O.
    """
    source, access_fence = await _lock_source_row(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=expected_access_fence,
    )
    if source is None:
        return None
    current = await session.scalar(
        select(SourcePurgeOperation).where(
            SourcePurgeOperation.source_id == source_id, *_operation_scope(scope),
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
        workspace_id=scope.workspace_id, actor_user_id=scope.user_id,
        membership_revision=scope.membership_revision,
        source_id=source_id, generation=source.generation, raw_uris=[],
        pending_owner_codes=["documents"],
    )
    session.add(operation)
    await session.flush()
    from modules.ingestion import public as ingestion
    await _fence_connector_source(
        session, source, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    now = datetime.now(UTC)
    event = DomainEvent(
        id=uuid4(), type="source.purge.requested", version=1, occurred_at=now,
        producer="modules.sources", payload={
            "operation_id": str(operation.id), "workspace_id": str(operation.workspace_id),
            "actor_user_id": operation.actor_user_id, "membership_revision": operation.membership_revision,
            "source_id": str(operation.source_id), "source_generation": operation.generation,
        },
    )
    await ingestion.publish_event(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    drafts = [make_source_change(source.id, source.generation, source.status, operation_id=operation.id, scope=scope)]
    await commit_with_replay(
        session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
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
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return <=100 exact unfinished purge operation IDs for one Source, keyset by ID.

    Documents uses this detached identity list to wake observers of a historical (unlinked)
    receipt without touching Source models. No ordering of unrelated operations is exposed.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Source observer page size must be between 1 and 100")
    statement = select(SourcePurgeOperation.id).where(
        SourcePurgeOperation.source_id == source_id, *_operation_scope(scope), *_open_coverage_operations(),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple((await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all())


async def pending_source_coverage_ids(
    session: AsyncSession, *, scope: InternalJobScope, multi_workspace_enabled: bool, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return one bounded keyset page of purge operations needing coverage reconciliation."""
    if not isinstance(scope, InternalJobScope):
        raise TypeError("Source coverage reconciliation requires an internal scope")
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Source coverage reconciliation page size must be between 1 and 100")
    # A failed purge whose Memory stage is done is terminal for polling; it re-settles only through
    # Documents/observer wakeups, which still select it via _open_coverage_operations.
    statement = select(SourcePurgeOperation.id).where(
        *_open_coverage_operations(),
        *_operation_scope(scope),
        ~and_(
            SourcePurgeOperation.status == "failed",
            SourcePurgeOperation.memory_status == "succeeded",
            SourcePurgeOperation.memory_cache_pending.is_(False),
        ),
    )
    if after is not None:
        statement = statement.where(SourcePurgeOperation.id > after)
    return tuple((await session.scalars(statement.order_by(SourcePurgeOperation.id).limit(limit))).all())


async def source_data_purge_exists(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> bool:
    """Report whether any data purge (in any state) ever fenced this Source.

    Export eligibility reopens when a purge succeeds; copied-evidence producers must not, so they
    use this durable fence instead. Callers hold the Source lock, which serializes this read with
    ``start_source_purge``.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    # A purge of another generation still fences copied evidence forever in this workspace.
    if isinstance(scope, InternalJobScope) and scope.source_id not in (None, source_id):
        return False
    return await session.scalar(
        select(SourcePurgeOperation.id).where(
            SourcePurgeOperation.source_id == source_id,
            SourcePurgeOperation.workspace_id == scope.workspace_id,
        ).limit(1)
    ) is not None


async def read_source_purge_operation(
    session: AsyncSession,
    operation_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> OperationRead | None:
    """Return the allowlisted public progress projection for one Source purge receipt.

    The exact Source-owned ID lookup exposes aggregate stage/count fields only; it never returns
    legacy URI JSON, child receipt IDs, event payloads, or owner-private cleanup data.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    operation = await session.scalar(select(SourcePurgeOperation).where(
        SourcePurgeOperation.id == operation_id, *_operation_scope(scope),
    ).execution_options(populate_existing=True))
    if operation is None:
        return None
    return OperationRead(
        operation_id=operation.id,
        workspace_id=operation.workspace_id,
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


async def _fence_connector_source(
    session: AsyncSession, source: Source, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> None:
    """Prepare then retire exact-source work with the original admitted access, flush only.

    Caller holds admission and exact Source, including its authorized transition once.
    Flush that transition and use only Source's constrained successor scope/current fence.
    Connector provisioning/all slots/browser/archived native precede tokens, Tools jobs and
    pages. Prepared token revoke and singular browser purge precede Connector continuation,
    which alone owns browser revision, provisioning/retirement and late hints/capacity.
    Missing optional rows never skip other cleanup. No earlier reentry, intermediate commit
    or provider I/O occurs; caller owns final replay with the original AccessFence.
    """
    from modules.connectors import public as connectors
    from modules.ingestion import public as ingestion
    from modules.tools.public import lock_source_browser_results_in_uow, purge_source_browser_results_in_uow

    scope = _transitioned_source_scope(scope, source)
    await session.flush()
    source_fence = SourceFence(
        id=source.id, workspace_id=source.workspace_id, status=source.status,
        generation=source.generation, local_only=source.local_only,
    )
    await connectors.prepare_source_lifecycle_cleanup_in_uow(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await ingestion.lock_source_credentials_in_uow(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await lock_source_browser_results_in_uow(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await ingestion.revoke_source_credentials(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await purge_source_browser_results_in_uow(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    await connectors.apply_source_lifecycle_cleanup_in_uow(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def _reconcile_github_hint_lifecycle(
    session: AsyncSession, source: Source, *, scope: Scope, multi_workspace_enabled: bool, active: bool,
) -> None:
    """Continue the held source/provisioning lifecycle transaction into GitHub hint ownership.

    The connector owner acquires the GitHub grant before hints and capacity. A source is rearmed
    only for an explicit activation carrying a current verified grant; access-loss pauses stay
    paused until the owner reconnects and explicitly resumes the source.
    """
    from modules.connectors import public as connectors

    await connectors.reconcile_github_source_hints_lifecycle(
        session, source_id=source.id, source_generation=source.generation, active=active,
        scope=_transitioned_source_scope(scope, source), multi_workspace_enabled=multi_workspace_enabled,
    )


def _transitioned_source_scope(scope: Scope, source: Source) -> Scope:
    """Carry one verified in-transaction Source generation transition into cleanup callees.

    This never changes actor/workspace/membership. Entry points first lock the exact captured
    generation; only the same or next generation is permitted, never revival of a stale job.
    """
    _require_source_owner(scope)
    if scope.workspace_id != source.workspace_id:
        raise ValueError("Source workspace changed")
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        assert scope.source_generation is not None
        if scope.source_id != source.id or source.generation not in (
            scope.source_generation, scope.source_generation + 1,
        ):
            raise ValueError("Source lifecycle transition does not match its captured generation")
        return replace(scope, source_generation=source.generation)
    return scope


async def get_connector_retained_effect_anchor_in_uow(
    session: AsyncSession, source_id: UUID, *, source_generation: int,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> SourceFence | None:
    """Read lifecycle metadata solely to settle Connector's exact retained remote effect.

    Caller holds original admission/Source before Connector operation locks. Require the
    unchanged original AccessFence and, for a bound worker, this exact Source/original G.
    A later generation of that same Source is visible only as lifecycle fence metadata:
    no current configuration, upgraded Scope, collection or activation authority. Connector
    must separately prove its immutable retained operation/step/target. No locks/commit/I/O;
    revoked access raises, missing/foreign/earlier-generation anchor returns None.
    """
    current_access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current_access != access_fence:
        raise HTTPException(status_code=409, detail="Retained Connector access fence changed")
    if type(source_generation) is not int or source_generation <= 0:
        raise ValueError("Original Connector Source generation is required")
    if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
        scope.source_id != source_id or scope.source_generation != source_generation
    ):
        raise ValueError("Retained effect does not match the original bound Source")
    row = (await session.execute(select(
        Source.id, Source.workspace_id, Source.status, Source.generation, Source.local_only,
    ).where(
        Source.id == source_id, Source.workspace_id == scope.workspace_id,
        Source.generation >= source_generation,
    ))).one_or_none()
    return SourceFence(**row._mapping) if row is not None else None


async def lock_connector_retained_effect_anchor(
    session: AsyncSession, source_id: UUID, *, source_generation: int,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> SourceFence | None:
    """Acquire original admission then this Source for exact Connector effect settlement.

    Enter before domain locks; require original access/config epoch without renewal.
    Lock only the original workspace/Source identity even when its generation advanced.
    Return lifecycle metadata only through the held owner contract; caller retains locks
    through cleanup/disposition commit and releases them before network. No commit/I/O.
    """
    await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        lock=True, expected=access_fence,
    )
    if type(source_generation) is not int or source_generation <= 0:
        raise ValueError("Original Connector Source generation is required")
    if isinstance(scope, InternalJobScope) and scope.source_id is not None and (
        scope.source_id != source_id or scope.source_generation != source_generation
    ):
        raise ValueError("Retained effect does not match the original bound Source")
    identifier = await session.scalar(select(Source.id).where(
        Source.id == source_id, Source.workspace_id == scope.workspace_id,
        Source.generation >= source_generation,
    ).with_for_update())
    if identifier is None:
        return None
    return await get_connector_retained_effect_anchor_in_uow(
        session, source_id, source_generation=source_generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )


async def discover_source_job_identity(
    session: AsyncSession, source_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Discover one real owned-default scheduler identity without acquiring row locks.

    Global fanout pages may call this repeatedly in one read transaction. Fresh owner,
    account/default-workspace/membership checks and an exact Source identity reread
    establish an identity snapshot only; no authorize_internal_job, commit or network.
    Each eventual binding must independently lock/admit its captured original access,
    Source generation and configuration before any effect. No metadata is returned.
    """
    identity = (await session.execute(select(Source.workspace_id, Source.generation).where(
        Source.id == source_id,
    ))).one_or_none()
    if identity is None:
        return None
    owner = await workspaces.resolve_workspace_owner_context(
        session, identity.workspace_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    scope = InternalJobScope(
        workspace_id=identity.workspace_id, actor_user_id=owner.user_id,
        membership_revision=owner.membership_revision, source_id=source_id,
        source_generation=identity.generation,
    )
    current = await session.scalar(select(Source.id).where(
        Source.id == source_id, *_source_scope(scope),
    ))
    return scope if current is not None else None


async def resolve_source_job_scope(
    session: AsyncSession, source_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve internal scheduler lineage only, then lock admission before Source reread.

    Discovery returns no metadata/configuration and never borrows owner authority for HTTP.
    Deferred jobs compare their original captured claim to this result; they cannot upgrade it.
    Caller owns rollback/commit and releases locks before collection or other external work.
    """
    identity = (await session.execute(select(Source.workspace_id, Source.generation).where(
        Source.id == source_id,
    ))).one_or_none()
    if identity is None:
        return None
    owner = await workspaces.resolve_workspace_owner_context(
        session, identity.workspace_id, multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    try:
        scope = InternalJobScope(
            workspace_id=identity.workspace_id, actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision, source_id=source_id,
            source_generation=identity.generation,
        )
    except ValueError:
        return None
    await workspaces.authorize_internal_job(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current = await session.scalar(select(Source.id).where(
        Source.id == source_id, *_source_scope(scope),
    ))
    return scope if current is not None else None


async def read_source_purge_job_identity(
    session: AsyncSession, operation_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Read one exact scoped retained purge identity without acquiring locks or live Source.

    Internal publication callers retain their ordered admission/domain transaction and compare
    operation ID plus workspace/actor/membership/source/generation to the event payload. Current
    caller admission and captured receipt membership must match; never upgrade an old job.
    No URI, child cleanup ID, content or public status actor/epoch is exposed; no commit/I/O.
    """
    await _admit_source_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    identity = (await session.execute(select(
        SourcePurgeOperation.workspace_id, SourcePurgeOperation.actor_user_id,
        SourcePurgeOperation.membership_revision, SourcePurgeOperation.source_id,
        SourcePurgeOperation.generation,
    ).where(
        SourcePurgeOperation.id == operation_id, *_operation_scope(scope),
        SourcePurgeOperation.membership_revision == scope.membership_revision,
    ))).one_or_none()
    if identity is None:
        return None
    try:
        return InternalJobScope(
            workspace_id=identity.workspace_id, actor_user_id=identity.actor_user_id,
            membership_revision=identity.membership_revision, source_id=identity.source_id,
            source_generation=identity.generation,
        )
    except ValueError:
        return None


async def resolve_source_purge_job_scope(
    session: AsyncSession, operation_id: UUID, *, multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Admit one retained purge's exact durable actor/revision/source-generation identity.

    No live Source is required and no URI/content/child receipt is exposed. Invalid lineage
    returns None; current permission loss propagates admission failure without rebasing the job.
    Ordered auth/workspace/membership locks precede the nonlocking retained identity reread;
    no domain lock is taken, so workers can subsequently acquire Source before receipt locks.
    Worker and already-authorized operator status callers own transaction release/quarantine.
    """
    columns = (
        SourcePurgeOperation.workspace_id, SourcePurgeOperation.actor_user_id,
        SourcePurgeOperation.membership_revision, SourcePurgeOperation.source_id,
        SourcePurgeOperation.generation,
    )
    identity = (await session.execute(select(*columns).where(
        SourcePurgeOperation.id == operation_id,
    ))).one_or_none()
    if identity is None:
        return None
    try:
        scope = InternalJobScope(
            workspace_id=identity.workspace_id, actor_user_id=identity.actor_user_id,
            membership_revision=identity.membership_revision, source_id=identity.source_id,
            source_generation=identity.generation,
        )
    except ValueError:
        return None
    await workspaces.authorize_internal_job(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current = (await session.execute(select(*columns).where(
        SourcePurgeOperation.id == operation_id, *_operation_scope(scope),
        SourcePurgeOperation.membership_revision == scope.membership_revision,
    ))).one_or_none()
    return scope if current is not None and current == identity else None
