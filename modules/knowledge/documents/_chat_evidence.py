"""Chat evidence reads: bounded, scope-qualified chunk projections and their locked variant."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents._news import (
    _validate_gadget_document_selection_fences_admitted,
    validate_gadget_document_selection_fences,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    GadgetDocumentSelectionFence,
)
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source


@dataclass(frozen=True)
class ChatEvidenceChunk:
    """Detached evidence chunk with full content and source privacy metadata for chat retrieval.

    Attributes:
        document_id: Owning document UUID.
        document_version_id: Revision UUID.
        version_number: Document version integer.
        chunk_id: Chunk UUID.
        chunk_index: Position index within document.
        content: Full text of the chunk for grounding and quote validation.
        source_id: Origin source UUID.
        source_name: Human-readable source name.
        source_status: Source status (e.g. 'active', 'paused', 'archived').
        source_generation: Ingestion/sync generation counter.
        local_only: Whether the source is restricted to local processing.
        title: Normalized version title or document title.
        canonical_url: Canonical document/version URL if available.
        metadata_is_version_snapshot: True if title/URL were captured from revision provenance.
        observed_at: Version observation timestamp.
        published_at: Document publication timestamp if known.
    """

    document_id: UUID
    document_version_id: UUID
    version_number: int
    chunk_id: UUID
    chunk_index: int
    content: str
    source_id: UUID
    source_name: str
    source_status: str
    source_generation: int
    local_only: bool
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    observed_at: datetime
    published_at: datetime | None


async def read_chat_evidence_chunks(
    session: AsyncSession,
    refs: list[tuple[UUID, UUID]],
    *,
    require_active_source: bool = True,
    require_current_version: bool = False,
    selection_fences: tuple[GadgetDocumentSelectionFence, ...] | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ChatEvidenceChunk]:
    """Read bounded detached evidence chunks with exact content and source privacy fence.

    Owner: modules/knowledge/documents
    Fields: document_id, document_version_id, version_number, chunk_id, chunk_index,
            content, source_id, source_name, source_status, source_generation, local_only,
            title, canonical_url, metadata_is_version_snapshot, observed_at, published_at.
    Permissions & Deletion checks:
        Enforces unique references bounded to 100 items. When require_active_source is True,
        restricts to Source.status == 'active'. Revalidates NormalizedVersionProvenance for
        immutable revision metadata snapshots. Exact gadget selections pass server-derived source
        fences, which are checked and locked before content chunks are read.

    Args:
        session: Active database session.
        refs: Unique list of (document_version_id, chunk_id) tuples.
        require_active_source: Whether to filter out chunks belonging to inactive sources.
        selection_fences: Server-derived source generation and provider-scope checks, when selected.

    Returns:
        List of detached ChatEvidenceChunk DTOs in the order of valid matching refs.

    Raises:
        ValueError: If refs list exceeds 100 items or contains duplicates. Exact selected reads also
            fail when a requested reference is missing instead of silently shrinking the selection.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, allow_member=True
    )
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if selection_fences is not None and not await validate_gadget_document_selection_fences(
        session,
        selection_fences,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")

    statement = (
        select(Document, DocumentVersion, DocumentChunk, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs),
            *_document_scope(scope, allow_member=True),
        )
    )
    if require_active_source:
        statement = statement.where(Source.status == "active")
    if require_current_version:
        statement = statement.where(Document.current_version == DocumentVersion.version_number)

    rows = (await session.execute(statement)).all()
    if not rows:
        if selection_fences is not None:
            raise ValueError("Evidence reference is missing or unavailable")
        return []

    provenance_rows = (
        await session.scalars(
            select(NormalizedVersionProvenance)
            .where(
                NormalizedVersionProvenance.document_version_id.in_(
                    {version.id for _, version, _, _ in rows}
                )
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    provenance_by_version = {item.document_version_id: item for item in provenance_rows}

    by_ref = {}
    for doc, ver, chunk, src in rows:
        prov = provenance_by_version.get(ver.id)
        by_ref[(ver.id, chunk.id)] = ChatEvidenceChunk(
            document_id=doc.id,
            document_version_id=ver.id,
            version_number=ver.version_number,
            chunk_id=chunk.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            source_id=src.id,
            source_name=src.name,
            source_status=src.status,
            source_generation=src.generation,
            local_only=src.local_only,
            title=prov.title if prov else doc.title,
            canonical_url=prov.canonical_url if prov else doc.canonical_url,
            metadata_is_version_snapshot=prov is not None,
            observed_at=ver.observed_at,
            published_at=doc.published_at,
        )

    if selection_fences is not None and set(by_ref) != set(refs):
        raise ValueError("One or more exact selected evidence chunks are unavailable")

    # Return matching items in the caller's requested order, omitting any deleted/missing refs.
    return [by_ref[ref] for ref in refs if ref in by_ref]


@dataclass(frozen=True)
class ChatEvidenceLockState:
    """Content-free lifecycle state of one locked evidence chunk (ids, generation, status, privacy flag).

    Attributes mirror the ``ChatEvidenceChunk`` fields that publication fences compare, without the
    chunk text, titles or provenance, so a per-flush revalidation never materializes ORM rows or content.
    """

    document_version_id: UUID
    chunk_id: UUID
    source_id: UUID
    source_generation: int
    source_status: str
    local_only: bool


async def _lock_evidence_rows(
    session: AsyncSession,
    refs: list[tuple[UUID, UUID]],
    *,
    selection_fences: tuple[GadgetDocumentSelectionFence, ...] | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Admit, validate and take Source -> Document -> version -> chunk key-share locks for ``refs``.

    Shared by the content and the lock-only readers so both keep the identical lock order. Returns
    False (nothing locked) for an empty ``refs``; raises ValueError for bad/duplicate references or a
    stale exact selection. Callers own commit/rollback, which releases every lock.
    """
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return False
    if (
        selection_fences is not None
        and not await _validate_gadget_document_selection_fences_admitted(
            session,
            selection_fences,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
            lock_rows=False,
            max_documents=32,
        )
    ):
        raise ValueError("Selected document version or source privacy scope is stale")
    ref_filter = tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs)
    scoped_refs = list(
        (
            await session.execute(
                select(Source.id, Document.id, DocumentVersion.id, DocumentChunk.id)
                .join(Document, Document.source_id == Source.id)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
                .where(ref_filter, *_document_scope(scope))
                .order_by(Source.id, Document.id, DocumentVersion.id, DocumentChunk.id)
            )
        ).all()
    )
    source_ids = sorted({row[0] for row in scoped_refs}, key=str)
    document_ids = sorted({row[1] for row in scoped_refs}, key=str)
    version_ids = sorted({row[2] for row in scoped_refs}, key=str)
    chunk_ids = sorted({row[3] for row in scoped_refs}, key=str)
    if source_ids:
        await sources.lock_source_set(
            session,
            source_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
    if document_ids:
        await session.scalars(
            select(Document.id)
            .where(Document.id.in_(document_ids), *_document_scope(scope))
            .order_by(Document.id)
            .with_for_update(read=True, key_share=True, of=Document)
        )
    if version_ids:
        await session.scalars(
            select(DocumentVersion.id)
            .where(DocumentVersion.id.in_(version_ids))
            .order_by(DocumentVersion.id)
            .with_for_update(read=True, key_share=True, of=DocumentVersion)
        )
    if chunk_ids:
        await session.scalars(
            select(DocumentChunk.id)
            .where(DocumentChunk.id.in_(chunk_ids))
            .order_by(DocumentChunk.id)
            .with_for_update(read=True, key_share=True, of=DocumentChunk)
        )
    if (
        selection_fences is not None
        and not await _validate_gadget_document_selection_fences_admitted(
            session,
            selection_fences,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
            lock_rows=False,
            max_documents=32,
        )
    ):
        raise ValueError("Selected document version or source privacy scope is stale")
    return True


async def lock_chat_evidence_chunks(
    session: AsyncSession,
    refs: list[tuple[UUID, UUID]],
    *,
    require_active_source: bool = True,
    require_current_version: bool = False,
    selection_fences: tuple[GadgetDocumentSelectionFence, ...] | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ChatEvidenceChunk]:
    """Hold key-share locks on exact evidence through Chat's short publication transaction.

    Retained scoped identities are captured before locking in Source, Document, immutable version,
    then chunk order. A hard delete cannot commit between this current-evidence check and the
    caller's publication commit; callers must release locks promptly by committing or rolling back.
    """
    if not await _lock_evidence_rows(
        session,
        refs,
        selection_fences=selection_fences,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return []
    evidence = await read_chat_evidence_chunks(
        session,
        refs,
        require_active_source=require_active_source,
        require_current_version=require_current_version,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if selection_fences is not None and len(evidence) != len(refs):
        raise ValueError("One or more exact selected evidence chunks are unavailable")
    return evidence


async def lock_chat_evidence_state(
    session: AsyncSession,
    refs: list[tuple[UUID, UUID]],
    *,
    require_active_source: bool = True,
    require_current_version: bool = False,
    selection_fences: tuple[GadgetDocumentSelectionFence, ...] | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ChatEvidenceLockState]:
    """Lock-only variant of ``lock_chat_evidence_chunks`` for per-flush publication fences.

    Takes the same Source -> Document -> version -> chunk key-share locks (deletion cannot commit
    before the caller's publication commit) and then reads one content-free column projection of
    ``(version, chunk, source id/generation/status/local_only)`` for the same filters: no Document,
    Source or chunk ORM rows, no chunk text and no provenance query. Same ValueError contract for a
    stale exact selection; refs in caller order, deleted/inactive ones omitted.
    """
    if not await _lock_evidence_rows(
        session,
        refs,
        selection_fences=selection_fences,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return []
    statement = (
        select(
            DocumentVersion.id,
            DocumentChunk.id,
            Source.id,
            Source.generation,
            Source.status,
            Source.local_only,
        )
        .select_from(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs),
            *_document_scope(scope, allow_member=True),
        )
    )
    if require_active_source:
        statement = statement.where(Source.status == "active")
    if require_current_version:
        statement = statement.where(Document.current_version == DocumentVersion.version_number)
    by_ref = {
        (row[0], row[1]): ChatEvidenceLockState(row[0], row[1], row[2], row[3], row[4], row[5])
        for row in (await session.execute(statement)).all()
    }
    if selection_fences is not None and set(by_ref) != set(refs):
        raise ValueError("One or more exact selected evidence chunks are unavailable")
    return [by_ref[ref] for ref in refs if ref in by_ref]
