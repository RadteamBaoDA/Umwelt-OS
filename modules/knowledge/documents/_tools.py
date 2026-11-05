"""Tool-facing document reads and output-fence revalidation."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    desc,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from core.tools.schemas import ToolDestination, ToolOutputFence
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentVersion,
)
from modules.sources.public import SourceReadProjection as Source


@dataclass(frozen=True)
class ToolDocumentRead:
    """Detached current-version document metadata exposed to the registered tool owner."""
    id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: int
    title: str
    content_type: str | None
    version_number: int
    created_at: datetime


@dataclass(frozen=True)
class ToolDocumentPage:
    """Carry a bounded detached document page and opaque continuation cursor."""
    items: tuple[ToolDocumentRead, ...]
    next_cursor: str | None


async def get_tool_document(
    session: AsyncSession, document_id: UUID, *, source_ids: frozenset[UUID],
    scope: Scope, multi_workspace_enabled: bool,
    owner_all: bool = False, destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolDocumentRead | None:
    """Read a query-time active/current document DTO under exact source and destination fences.

    Local-only source rows are excluded in SQL for remote destinations. An empty non-owner
    source set returns no rows. This query-time projection does not replace revalidation by
    the eventual sender immediately before remote transmission. ``owner_all`` means every
    Source of the admitted workspace, never other workspaces.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = (
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, Document.title,
               Document.content_type, DocumentVersion.version_number, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            *_document_scope(scope),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
        )
    )
    if not owner_all:
        if not source_ids:
            return None
        statement = statement.where(Document.source_id.in_(source_ids))
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    row = (await session.execute(statement)).one_or_none()
    return ToolDocumentRead(*row) if row is not None else None


async def revalidate_tool_document_fences(
    session: AsyncSession,
    fences: Sequence[ToolOutputFence],
    *,
    source_ids: frozenset[UUID],
    scope: Scope,
    multi_workspace_enabled: bool,
    owner_all: bool = False,
    destination: ToolDestination = ToolDestination.REMOTE,
) -> bool:
    """Require every bounded native Knowledge result to remain an exact current document DTO.

    A single missing, changed, out-of-scope, inactive, unready or remote-local-only row denies the
    full page, including its cursor. The projection returns no persistence models or write access.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(fences) > 100:
        return False
    expected: dict[UUID, tuple[UUID, UUID, int]] = {}
    for fence in fences:
        if (
            not isinstance(fence, ToolOutputFence)
            or not isinstance(fence.document_id, UUID)
            or not isinstance(fence.document_version_id, UUID)
            or not isinstance(fence.source_id, UUID)
            or type(fence.source_generation) is not int or fence.source_generation < 1
            or fence.chunk_id is not None
        ):
            return False
        if not owner_all and fence.source_id not in source_ids:
            return False
        if fence.document_id in expected:
            return False
        expected[fence.document_id] = (
            fence.document_version_id, fence.source_id, fence.source_generation,
        )
    if not expected:
        return True
    statement = (
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id.in_(expected),
            *_document_scope(scope),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
        )
    )
    if not owner_all:
        if not source_ids:
            return False
        statement = statement.where(Document.source_id.in_(source_ids))
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    rows = (await session.execute(statement)).all()
    current = {
        document_id: (version_id, source_id, generation)
        for document_id, version_id, source_id, generation in rows
    }
    return current == expected


async def list_tool_documents(
    session: AsyncSession, *, limit: int, cursor: str | None,
    source_ids: frozenset[UUID], scope: Scope, multi_workspace_enabled: bool,
    owner_all: bool = False, destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolDocumentPage:
    """Page only active/current rows allowed by source and destination before cursor creation.

    Remote local-only rows are filtered in SQL before limit/keyset selection, so no hidden
    source/document identifier participates in the returned page or continuation cursor.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Document tool page size is outside its supported bound")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = (
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, Document.title,
               Document.content_type, DocumentVersion.version_number, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            *_document_scope(scope),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")), Source.status == "active",
        )
    )
    if not owner_all:
        if not source_ids:
            return ToolDocumentPage((), None)
        statement = statement.where(Document.source_id.in_(source_ids))
    if destination != ToolDestination.LOCAL:
        statement = statement.where(Source.local_only.is_(False))
    if cursor:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Document.created_at, Document.id) < (timestamp, identifier))
    rows = list((await session.execute(
        statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1)
    )).all())
    more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if more and page else None
    return ToolDocumentPage(tuple(ToolDocumentRead(*row) for row in page), next_cursor)
