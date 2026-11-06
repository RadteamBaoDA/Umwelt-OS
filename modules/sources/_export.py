"""Source paging cursors, export pages, export-fence validation and export eligibility."""

import binascii
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Integer, Select, case, cast, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.errors import ForbiddenError, ValidationFailedError
from core.export_pages import content_digest, dto_digest, serialize_bounded_page
from core.pagination import b64url_decode, b64url_encode, decode_cursor, encode_cursor
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.sources._scope import _admit_source_scope, _source_scope
from modules.sources.models import Source, SourcePurgeOperation
from modules.sources.schemas import (
    SourceExportFence,
    SourceMetadataExportFence,
    SourceMetadataExportPage,
    SourceMetadataExportValidation,
    SourceRead,
)


def _cursor_fingerprint(fence: AccessFence, scope: Scope, *filters: object) -> str:
    """Fingerprint actual actor/workspace/revisions plus exact selector and ordering inputs."""
    values = [
        str(fence.workspace_id),
        fence.user_id,
        fence.membership_revision,
        fence.configuration_revision,
        *filters,
    ]
    if isinstance(scope, InternalJobScope):
        values += [str(scope.source_id) if scope.source_id else None, scope.source_generation]
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _encode_source_page_cursor(created_at: datetime, identifier: UUID, fingerprint: str) -> str:
    """Wrap the existing keyset primitive with the exact admitted request fingerprint."""
    raw = json.dumps(
        [2, fingerprint, encode_cursor(created_at, identifier)], separators=(",", ":")
    ).encode()
    return b64url_encode(raw)


def _decode_source_page_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Reject legacy, foreign-principal, changed-filter and changed-revision continuations."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 3 or values[:2] != [2, fingerprint]:
            raise ValueError
        created_at, identifier = decode_cursor(values[2])
        if _encode_source_page_cursor(created_at, identifier, fingerprint) != cursor:
            raise ValueError
        return created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail="Source cursor is invalid") from exc


def _encode_source_export_cursor(
    snapshot_at: datetime,
    created_at: datetime,
    identifier: UUID,
    fingerprint: str,
) -> str:
    """Bind a canonical source metadata keyset position to one fixed owner snapshot."""
    snapshot_fingerprint = hashlib.sha256(
        f"{fingerprint}:{snapshot_at.isoformat()}".encode()
    ).hexdigest()
    payload = json.dumps(
        [
            2,
            "sources",
            snapshot_fingerprint,
            snapshot_at.isoformat(),
            created_at.isoformat(),
            str(identifier),
        ],
        separators=(",", ":"),
    ).encode()
    return b64url_encode(payload)


def _decode_source_export_cursor(cursor: str, fingerprint: str) -> tuple[datetime, datetime, UUID]:
    """Reject oversized, noncanonical, cross-dataset, or future source export cursors."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 6 or values[:2] != [2, "sources"]:
            raise ValueError
        snapshot_at, created_at = (
            datetime.fromisoformat(values[3]),
            datetime.fromisoformat(values[4]),
        )
        identifier = UUID(values[5])
        snapshot_fingerprint = hashlib.sha256(
            f"{fingerprint}:{snapshot_at.isoformat()}".encode()
        ).hexdigest()
        if (
            any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (snapshot_at, created_at)
            )
            or values[2] != snapshot_fingerprint
            or snapshot_at.isoformat() != values[3]
            or created_at.isoformat() != values[4]
            or created_at > snapshot_at
            or snapshot_at > datetime.now(UTC)
            or str(identifier) != values[5]
            or _encode_source_export_cursor(snapshot_at, created_at, identifier, fingerprint)
            != cursor
        ):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail="Source export cursor is invalid") from exc


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
        Source.id,
        Source.type,
        Source.name,
        Source.provider,
        Source.status,
        Source.local_only,
        Source.graph_enabled,
        Source.rag_index_enabled,
        Source.last_sync_at,
        Source.last_success_at,
        Source.last_error_at,
        Source.last_error_code,
        Source.collected_at,
        Source.indexed_at,
        Source.collection_error_code,
        Source.processing_error_code,
        Source.generation,
        Source.retired_at,
        Source.created_at,
        Source.updated_at,
    )


def _source_export_read(row: Any) -> SourceRead:
    """Build one safe source record from explicit columns, never loading connector configuration."""
    return SourceRead(**row._mapping)


async def export_page(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
) -> SourceMetadataExportPage:
    """Return a bounded source metadata export, excluding all provider configuration and secrets."""
    fence = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if owner_id != fence.user_id or record_kind != "sources" or not 1 <= limit <= 100:
        raise ValueError("Source export owner, kind or page limit is invalid")
    fingerprint = _cursor_fingerprint(
        fence, scope, "sources-export", "created_at,id:asc", record_kind, limit
    )
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_source_export_cursor(cursor, fingerprint)
        position = (position_at, position_id)
    snapshot_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Source)
            .where(*_source_export_scope(snapshot_at, scope=scope))
        )
        or 0
    )
    statement = select(*_source_export_columns()).where(
        *_source_export_scope(snapshot_at, scope=scope)
    )
    if position is not None:
        statement = statement.where(tuple_(Source.created_at, Source.id) > position)
    rows = list(
        (
            await session.execute(statement.order_by(Source.created_at, Source.id).limit(limit + 1))
        ).all()
    )
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [_source_export_read(row) for row in rows]
    encoded_items, payload_bytes = serialize_bounded_page(items, label="Source")
    fences = [
        SourceMetadataExportFence(
            source_id=row.id,
            workspace_id=scope.workspace_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            generation=row.generation,
            content_digest=content_digest(raw),
        )
        for row, raw in zip(rows, encoded_items, strict=True)
    ]
    return SourceMetadataExportPage(
        owner_id=owner_id,
        workspace_id=scope.workspace_id,
        record_kind="sources",
        snapshot_at=snapshot_at,
        snapshot_count=snapshot_count,
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=_encode_source_export_cursor(
            snapshot_at, rows[-1].created_at, rows[-1].id, fingerprint
        )
        if has_more and rows
        else None,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[SourceMetadataExportFence],
) -> SourceMetadataExportValidation:
    """Recheck source eligibility, generation, row content, and fixed-cutoff inventory."""
    access = await _admit_source_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if owner_id != access.user_id or record_kind != "sources" or len(fences) > 100:
        raise ValueError("Source export validation input is invalid")
    if any(fence.workspace_id != scope.workspace_id for fence in fences):
        return SourceMetadataExportValidation(
            valid=False, reason="record_changed", observed_snapshot_count=0
        )
    observed = int(
        await session.scalar(
            select(func.count())
            .select_from(Source)
            .where(*_source_export_scope(snapshot_at, scope=scope))
        )
        or 0
    )
    if observed != expected_snapshot_count:
        return SourceMetadataExportValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    source_fences = [
        SourceExportFence(
            source_id=item.source_id,
            workspace_id=item.workspace_id,
            generation=item.generation,
        )
        for item in fences
    ]
    if len(
        await filter_export_eligible_sources(
            session,
            source_fences,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    ) != len(source_fences):
        return SourceMetadataExportValidation(
            valid=False, reason="record_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        row = (
            await session.execute(
                select(*_source_export_columns()).where(
                    Source.id == fence.source_id,
                    *_source_export_scope(snapshot_at, scope=scope),
                )
            )
        ).one_or_none()
        if row is None:
            return SourceMetadataExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        item = _source_export_read(row)
        if (
            item.generation != fence.generation
            or item.created_at != fence.created_at
            or item.updated_at != fence.updated_at
            or dto_digest(item) != fence.content_digest
        ):
            return SourceMetadataExportValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return SourceMetadataExportValidation(
        valid=True, reason="valid", observed_snapshot_count=observed
    )


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
        SourcePurgeOperation.status.in_(("queued", "running", "failed")),
    )
    # Member branch: workspace-only; Documents adds the grant predicate. No Source config is read.
    scoped = (
        (Source.workspace_id == scope.workspace_id,)
        if isinstance(scope, WorkspaceContext) and scope.role != "owner"
        else _source_scope(scope)
    )
    return select(Source.id).where(*scoped, ~Source.id.in_(pending_data_purges))


async def filter_export_eligible_sources(
    session: AsyncSession,
    fences: Sequence[SourceExportFence],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
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
    rows = (
        await session.execute(
            select(Source.id, Source.generation).where(
                Source.id.in_(source_ids), Source.id.in_(export_eligible_source_ids(scope=scope))
            )
        )
    ).all()
    return tuple(
        source_id for source_id, generation in rows if generations.get(source_id) == generation
    )


async def observability_quality_summary(
    session: AsyncSession,
    *,
    instance_operator: bool,
    now: datetime | None = None,
) -> dict[str, int]:
    """Aggregate instance health only for an already-admitted bootstrap operator/internal caller.

    The capability flag is internal, never supplied by an HTTP body or workspace owner;
    O owns operator admission before calling this deliberate global, content-free projection.
    """
    if instance_operator is not True:
        raise ForbiddenError(detail="Instance operator required")
    now = now or datetime.now(UTC)
    cadence = cast(Source.configuration["schedule_interval_minutes"].astext, Integer)
    default_cadence = case((Source.type == "rss", 15), else_=30)
    stale_sources = int(
        await session.scalar(
            select(func.count())
            .select_from(Source)
            .where(
                Source.status == "active",
                Source.type.in_(("rss", "web", "api")),
                func.coalesce(Source.last_success_at, Source.created_at)
                < now
                - func.make_interval(
                    0, 0, 0, 0, 0, 0, func.coalesce(cadence, default_cadence) * 120
                ),
            )
        )
        or 0
    )
    return {"stale_sources": stale_sources}
