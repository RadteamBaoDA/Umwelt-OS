"""Evidence-reference reads and locked review-version evidence for downstream owners."""

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
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    EvidenceReferenceRead,
)
from modules.sources.models import Source


async def _list_evidence_ref_keys(
    session: AsyncSession, *, scope: Scope, source_id: UUID, document_id: UUID | None = None,
    limit: int = 10_000,
) -> tuple[list[tuple[UUID, UUID]], bool]:
    """List workspace-qualified version/chunk evidence keys for one Source or Document, with overflow.

    Discovery never raises on overflow: it returns the first ``limit`` sorted keys and True so the
    closure coordinator can report a dependency-limit error before any lock or effect.
    """
    if not 1 <= limit <= 10_000:
        raise ValueError("Specify a bounded limit")
    statement = (
        select(DocumentVersion.id, DocumentChunk.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.workspace_id == scope.workspace_id, Document.source_id == source_id)
    )
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    rows = list((await session.execute(statement.order_by(DocumentVersion.id, DocumentChunk.id).limit(limit + 1))).all())
    return [(version_id, chunk_id) for version_id, chunk_id in rows[:limit]], len(rows) > limit


@dataclass(frozen=True)
class ReviewEvidenceRef:
    """Carry retained evidence provenance and excerpts for owner correction review."""
    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    current_source_generation: int
    source_name: str
    version_number: int
    chunk_id: UUID
    title: str
    canonical_url: str | None
    metadata_is_version_snapshot: bool
    observed_at: datetime
    excerpt: str


async def read_evidence_refs(
    session: AsyncSession, refs: list[tuple[UUID, UUID]], *, scope: Scope,
    multi_workspace_enabled: bool, for_write: bool = False,
) -> list[EvidenceReferenceRead]:
    """Resolve unique bounded references and compare original admission before ordered write locks."""
    access_fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if for_write:
        from modules.sources import public as sources_public

        source_ids = list((await session.scalars(
            select(Source.id).join(Document, Document.source_id == Source.id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
            .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs), *_document_scope(scope))
            .distinct().order_by(Source.id)
        )).all())
        if source_ids:
            source_set = await sources_public.lock_source_set(
                session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            if len(source_set.fences) != len(source_ids):
                raise ValueError("Evidence source no longer exists")
        document_ids = list((await session.scalars(
            select(Document.id).join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
            .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs), *_document_scope(scope))
            .distinct().order_by(Document.id)
        )).all())
        await session.scalars(
            select(Document)
            .where(Document.id.in_(document_ids), *_document_scope(scope))
            .order_by(Document.id)
            .with_for_update()
        )
    return await _read_evidence_ref_rows(session, refs, scope=scope)


async def _read_evidence_ref_rows(
    session: AsyncSession, refs: list[tuple[UUID, UUID]], *, scope: Scope,
) -> list[EvidenceReferenceRead]:
    """Build fresh ordered evidence DTOs with version provenance and exact reference validation.

    populate_existing prevents a caller's stale identity-map objects from authorizing a deleted,
    replaced, or edited source citation during finalization or other evidence-bound reads.
    """
    rows = (await session.execute(
        select(Document, DocumentVersion, DocumentChunk, Source.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs), *_document_scope(scope))
        .execution_options(populate_existing=True)
    )).all()
    provenance_rows = (await session.scalars(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_version_id.in_(
                {version.id for _, version, _, _ in rows}
            )
        ).execution_options(populate_existing=True)
    )).all() if rows else []
    provenance_by_version = {item.document_version_id: item for item in provenance_rows}
    by_ref = {
        (version.id, chunk.id): EvidenceReferenceRead(
            document_id=document.id,
            document_version_id=version.id,
            version_number=version.version_number,
            chunk_id=chunk.id,
            source_id=source_id,
            title=provenance_by_version[version.id].title if version.id in provenance_by_version else document.title,
            canonical_url=(
                provenance_by_version[version.id].canonical_url
                if version.id in provenance_by_version else document.canonical_url
            ),
            metadata_is_version_snapshot=version.id in provenance_by_version,
            observed_at=version.observed_at,
            excerpt=chunk.content[:1000],
        )
        for document, version, chunk, source_id in rows
    }
    if set(by_ref) != set(refs):
        raise ValueError("Evidence reference is missing or does not match its document revision")
    return [by_ref[ref] for ref in refs]


async def lock_review_version_evidence(
    session: AsyncSession, *, document_id: UUID, source_id: UUID, version_id: UUID,
    source_generation: int, chunk_ids: list[UUID], scope: Scope, multi_workspace_enabled: bool,
) -> list[ReviewEvidenceRef] | None:
    """Fence a bounded owner correction to retained immutable evidence, including history."""
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not chunk_ids or len(chunk_ids) > 5 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Review evidence must contain unique bounded chunks")
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id, *_document_scope(scope))
        .with_for_update().execution_options(populate_existing=True)
    )
    if document is None:
        return None
    version = await session.scalar(select(DocumentVersion).where(
        DocumentVersion.id == version_id, DocumentVersion.document_id == document_id,
    ))
    source_row = (await session.execute(select(Source.generation, Source.name).where(Source.id == source_id))).one_or_none()
    generation, source_name = source_row if source_row else (None, None)
    if version is None or generation != source_generation:
        return None
    assert source_name is not None  # generation matched, so the source row exists
    refs = await read_evidence_refs(
        session, [(version_id, chunk_id) for chunk_id in chunk_ids],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(refs) != len(chunk_ids) or any(
        ref.document_id != document_id or ref.source_id != source_id for ref in refs
    ):
        return None
    return [ReviewEvidenceRef(
        document_id=ref.document_id,
        document_version_id=ref.document_version_id,
        source_id=ref.source_id,
        current_source_generation=generation,
        source_name=source_name,
        version_number=ref.version_number,
        chunk_id=ref.chunk_id,
        title=ref.title,
        canonical_url=ref.canonical_url,
        metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
        observed_at=ref.observed_at,
        excerpt=ref.excerpt,
    ) for ref in refs]
