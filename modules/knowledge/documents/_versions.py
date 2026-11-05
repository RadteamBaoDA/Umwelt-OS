"""Version listing/cursors and review-version fences."""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    desc,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ValidationFailedError
from core.pagination import decode_cursor, encode_cursor
from core.workspaces.schemas import (
    AdminWorkspaceReadContext,
    Scope,
    WorkspaceContext,
)
from modules.knowledge.documents._documents import (
    _decode_document_owner_cursor,
    _encode_document_owner_cursor,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupEvidenceReference,
    DocumentCleanupOperation,
    DocumentVersion,
)
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source


@dataclass(frozen=True)
class ReadyVersionRef:
    """Reference a ready current version with source generation and privacy state."""
    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: int
    version_number: int
    created_at: datetime
    local_only: bool
    raw_uri: str | None
    mime_type: str | None


@dataclass(frozen=True)
class ReviewVersionFence:
    """Snapshot document/source identity and current source generation for review."""
    document_id: UUID
    source_id: UUID
    current_source_generation: int
    source_name: str
    version_number: int


async def list_ready_version_refs(
    session: AsyncSession, limit: int = 50, cursor: str | None = None, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[ReadyVersionRef], str | None]:
    """Page through active-source current versions that have ready chunks."""
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if not 1 <= limit <= 100:
        raise ValueError("Ready-version page size must be between 1 and 100")
    statement = (
        select(
            Document.id, Document.created_at, Source.id, Source.generation,
            DocumentVersion.id, DocumentVersion.version_number, Source.local_only,
            Document.raw_uri, Document.mime_type,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            DocumentVersion.content != "",
            Source.status == "active",
            *_document_scope(scope),
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )
    if cursor:
        scoped_cursor = _decode_document_owner_cursor(
            cursor, kind="ready_versions", fence=access_fence, resource_id=None,
        )
        created_at, identifier = decode_cursor(scoped_cursor)
        statement = statement.where(tuple_(Document.created_at, Document.id) < (created_at, identifier))
    rows = list((await session.execute(statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    result = [ReadyVersionRef(
        document_id=document_id, document_version_id=version_id, source_id=source_id,
        source_generation=generation, version_number=version_number, created_at=created_at,
        local_only=local_only, raw_uri=raw_uri, mime_type=mime_type,
    ) for (document_id, created_at, source_id, generation, version_id, version_number, local_only,
           raw_uri, mime_type) in rows]
    next_cursor = (_encode_document_owner_cursor(
        encode_cursor(rows[-1][1], rows[-1][0]), kind="ready_versions",
        fence=access_fence, resource_id=None,
    ) if more and rows else None)
    return result, next_cursor


async def get_ready_version_ref(
    session: AsyncSession, version_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> ReadyVersionRef | None:
    """Resolve one version only while it remains the ready current version."""
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(
            Document.id, Document.created_at, Source.id, Source.generation,
            DocumentVersion.id, DocumentVersion.version_number, Source.local_only,
            Document.raw_uri, Document.mime_type,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentVersion.id == version_id,
            *_document_scope(scope),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            DocumentVersion.content != "",
            Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )).one_or_none()
    if row is None:
        return None
    (document_id, created_at, source_id, generation, actual_version_id, version_number, local_only,
     raw_uri, mime_type) = row
    return ReadyVersionRef(
        document_id=document_id, document_version_id=actual_version_id, source_id=source_id,
        source_generation=generation, version_number=version_number,
        created_at=created_at, local_only=local_only, raw_uri=raw_uri, mime_type=mime_type,
    )


def encode_version_cursor(version_number: int) -> str:
    """Encode a version number as canonical unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(str(version_number).encode()).decode().rstrip("=")


def decode_version_cursor(cursor: str) -> int:
    """Decode a canonical version cursor or raise HTTP 422 for invalid input."""
    try:
        if "=" in cursor:
            raise ValueError("Cursor must be unpadded")
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Cursor is not canonical URL-safe base64")
        version_number = int(raw)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationFailedError("Invalid cursor") from exc
    if not 1 <= version_number <= 2147483647:
        raise ValidationFailedError("Invalid cursor")
    return version_number


async def list_versions(
    session: AsyncSession, document_id: UUID, limit: int, cursor: str | None, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[DocumentVersion] | None, str | None]:
    """Page <=100 ascending immutable revisions through an admitted retained owner root.

    Workspace/Source deletion lineage is applied to parent and version query before LIMIT.
    Historical revisions remain exact, including inactive retained metadata; missing/foreign
    parent returns None. Members cannot enumerate versions. No lock, mutation or commit.
    """
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext)):
        raise TypeError("Document history requires a workspace owner")
    fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, allow_member=True)
    if not 1 <= limit <= 100:
        raise ValueError("Version page size must be between 1 and 100")
    after_version = decode_version_cursor(_decode_document_owner_cursor(
        cursor, kind="versions", fence=fence, resource_id=document_id)) if cursor is not None else None
    scoped = _document_scope(scope, allow_member=True)
    if await session.scalar(select(Document.id).where(Document.id == document_id, *scoped)) is None:
        return None, None
    statement = select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id).where(
        Document.id == document_id, *scoped,
    )
    if after_version is not None:
        statement = statement.where(DocumentVersion.version_number > after_version)
    result = await session.scalars(
        statement.order_by(DocumentVersion.version_number).limit(limit + 1)
    )
    rows = list(result.all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_document_owner_cursor(encode_version_cursor(rows[-1].version_number),
        kind="versions", fence=fence, resource_id=document_id) if has_more and rows else None
    return rows, next_cursor


async def get_version(
    session: AsyncSession, document_id: UUID, number: int, *, scope: Scope, multi_workspace_enabled: bool,
) -> DocumentVersion | None:
    """Read one exact historical owner revision through its retained scoped parent.

    Real owner admission precedes root/version predicates; inactive retained Source history
    remains visible except unfinished/failed data purges. No current-version substitution,
    member visibility, parent lock, mutation or commit; missing/foreign versions return None.
    """
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext)):
        raise TypeError("Document history requires a workspace owner")
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, allow_member=True)
    return await session.scalar(
        select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == number,
            *_document_scope(scope, allow_member=True),
        )
    )


async def review_version_locator(
    session: AsyncSession, version_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[UUID, UUID] | None:
    """Locate a retained version inside admitted exact workspace/Source scope only.

    Return detached (Document UUID, Source UUID), with no content or ORM. Owner
    public nonlocking admission checks current owner/default-workspace and any
    strict bound Source generation. Filter version/Document by workspace before
    Source projection; missing/foreign/deleted or missing Source returns None.
    Existing held callers acquire no earlier locks and supply the actual flag;
    this locator is read-only, never proof of write authority or retention locks.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(Document.id, Document.source_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == version_id, *_document_scope(scope))
    )).one_or_none()
    if row is None:
        return None
    source = await sources.get_source_fence(session, row[1], scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled)
    return (row[0], row[1]) if source is not None else None


async def cleanup_evidence_version_document(
    session: AsyncSession, version_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    source_id: UUID | None = None,
) -> UUID | None:
    """Return the Document that owned a version, from the retained cleanup evidence of a deleted Document.

    Fallback for callers whose live ``review_version_locator`` no longer resolves a version. Reads only
    immutable receipt references (no lock), so it cannot invert the privacy -> receipt -> owner order.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = (
        select(DocumentCleanupOperation.document_id)
        .join(DocumentCleanupEvidenceReference, DocumentCleanupEvidenceReference.operation_id == DocumentCleanupOperation.id)
        .where(
            DocumentCleanupOperation.workspace_id == scope.workspace_id,
            DocumentCleanupEvidenceReference.workspace_id == scope.workspace_id,
            DocumentCleanupEvidenceReference.document_version_id == version_id,
            DocumentCleanupEvidenceReference.reference_kind == "version",
        )
    )
    if source_id is not None:
        statement = statement.where(DocumentCleanupOperation.source_id == source_id)
    return await session.scalar(statement.order_by(DocumentCleanupOperation.id).limit(1))


async def review_version_fences(
    session: AsyncSession, version_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, ReviewVersionFence]:
    """Return source-generation snapshots for a bounded de-duplicated version set."""
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = list(dict.fromkeys(version_ids))
    if len(ids) > 100:
        raise ValueError("Review version fence set exceeds its page limit")
    if not ids:
        return {}
    rows = (await session.execute(
        select(DocumentVersion.id, Document.id, Document.source_id, Source.generation, Source.name, DocumentVersion.version_number)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentVersion.id.in_(ids), *_document_scope(scope))
    )).all()
    return {
        version_id: ReviewVersionFence(document_id, source_id, generation, source_name, version_number)
        for version_id, document_id, source_id, generation, source_name, version_number in rows
    }


async def lock_document_ids(
    session: AsyncSession, document_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Lock a bounded, sorted set of retained document rows for owner transactions."""
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = sorted(set(document_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Document lock set exceeds its atomic limit")
    if not ids:
        return []
    locked = list((await session.scalars(
        select(Document.id).where(Document.id.in_(ids), *_document_scope(scope))
        .order_by(Document.id).with_for_update()
    )).all())
    return locked
