"""Extraction input/status contracts: locking, status, saved extraction and evidence refs."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import (
    func,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._hashing import content_hash, content_hash_async
from modules.knowledge.documents._identity import _normalized_source_proof, lock_raw_uri_identity
from modules.knowledge.documents._ready import _publish_extraction_ready
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupOperation,
    DocumentVersion,
)
from modules.sources.public import SourceFence
from modules.sources.public import SourceReadProjection as Source

EXTRACTION_CHUNK_LIMIT = 100


EXTRACTION_INPUT_BYTES = 64_000


class ExtractionInputLimitError(ValueError):
    """Signal an immutable version outside extraction limits without exposing its content.

    Scheduling owners may persist blocked work while acknowledging canonical
    readiness. Existing ValueError handlers retain their validation behavior.
    """


@dataclass(frozen=True)
class ExtractionChunk:
    """Carry a chunk ID and its content as detached extraction input."""

    id: UUID
    content: str


@dataclass(frozen=True)
class ExtractionInput:
    """Snapshot the active current document version and source extraction policy."""

    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: int
    local_only: bool
    observed_at: datetime
    chunks: tuple[ExtractionChunk, ...]


@dataclass(frozen=True)
class ExtractionEvidenceRef:
    """Identify a chunk accepted as evidence under a source generation fence."""

    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    source_generation: int
    chunk_id: UUID


async def _extraction_document(
    session: AsyncSession,
    document_id: UUID,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    expected_raw_uri: str | None,
    expected_mime_type: str | None,
) -> Document | None:
    """Read one exact parser parent, comparing original URI/MIME and full fences.

    Early preparation uses this read before its singleton lock; late writes
    retain that Document/URI lock before Ingestion roots/outbox. No lock or authority fallback;
    missing/moved/deleted/raw-input mismatch returns None before mutation.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("extraction_original_fence_required")
    await _normalized_source_proof(
        session,
        source_id,
        source_fence.generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if expected_raw_uri is None:
        # Raw-less (API/manual content) document: no original input to prove; the stored
        # version content is authoritative. Still bound to exact workspace/Source/Document.
        if expected_mime_type is not None and (
            type(expected_mime_type) is not str or not 1 <= len(expected_mime_type) <= 255
        ):
            raise RuntimeError("extraction_original_input_required")
        return await session.scalar(
            select(Document)
            .where(
                Document.id == document_id,
                Document.source_id == source_id,
                Document.workspace_id == scope.workspace_id,
                Document.raw_uri.is_(None),
                Document.mime_type.is_(None)
                if expected_mime_type is None
                else Document.mime_type == expected_mime_type,
            )
            .execution_options(populate_existing=True)
        )
    if (
        type(expected_raw_uri) is not str
        or not expected_raw_uri
        or type(expected_mime_type) is not str
        or not 1 <= len(expected_mime_type) <= 255
    ):
        raise RuntimeError("extraction_original_input_required")
    document = await session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.source_id == source_id,
            Document.workspace_id == scope.workspace_id,
            Document.raw_uri == expected_raw_uri,
            Document.mime_type == expected_mime_type,
        )
        .execution_options(populate_existing=True)
    )
    if (
        document is None
        or await session.scalar(
            select(DocumentCleanupOperation.id)
            .where(
                DocumentCleanupOperation.raw_uri == expected_raw_uri,
            )
            .limit(1)
        )
        is not None
    ):
        return None
    return document


async def lock_document_for_extraction(
    session: AsyncSession,
    document_id: UUID,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    expected_raw_uri: str | None,
    expected_mime_type: str | None,
) -> bool:
    """Prepare singleton parser Document then URI lifecycle before any Ingestion roots.

    Caller retains original admitted workspace/Source locks. Freshly compare
    original complete fences, exact workspace/Source/Document/raw URI/MIME, lock
    the Document and then existing raw-URI lifecycle identity in deletion order.
    Missing/mismatching/deleted input returns False. No mutation, commit or I/O;
    Entities/Timeline/News callers require their own scoped owner conversion.
    """
    if (
        await _extraction_document(
            session,
            document_id,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
            expected_raw_uri=expected_raw_uri,
            expected_mime_type=expected_mime_type,
        )
        is None
    ):
        return False
    document = await session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.source_id == source_id,
            Document.workspace_id == scope.workspace_id,
            Document.raw_uri.is_(None)
            if expected_raw_uri is None
            else Document.raw_uri == expected_raw_uri,
            Document.mime_type.is_(None)
            if expected_mime_type is None
            else Document.mime_type == expected_mime_type,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if document is None:
        return False
    if expected_raw_uri is not None:
        await lock_raw_uri_identity(session, expected_raw_uri)
    return (
        await _extraction_document(
            session,
            document_id,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence,
            source_fence=source_fence,
            expected_raw_uri=expected_raw_uri,
            expected_mime_type=expected_mime_type,
        )
        is not None
    )


async def set_extraction_status(
    session: AsyncSession,
    document_id: UUID,
    source_id: UUID,
    status: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    expected_raw_uri: str,
    expected_mime_type: str,
) -> bool:
    """Flush parser status only on its already-held exact parent and original input.

    Retained early Document/URI locks and original admission/Source are mandatory;
    no earlier acquisition follows outbox. Invalid status is rejected before any
    mutation. Missing/deleted/mismatching input returns False, stale fences abort.
    """
    if status not in {"queued", "processing", "succeeded", "failed", "ready", "needs_ocr"}:
        raise ValueError("Invalid extraction status")
    document = await _extraction_document(
        session,
        document_id,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
        expected_raw_uri=expected_raw_uri,
        expected_mime_type=expected_mime_type,
    )
    if document is None:
        return False
    document.extraction_status = status
    await session.flush()
    return True


async def _append_text_version(
    session: AsyncSession, document: Document, text: str
) -> DocumentVersion:
    """Add the next immutable version holding ``text`` and make it the document's current one.

    Hashes the text (off-loop), derives the next monotonic version number, adds the row, updates the
    document's current version/hash and flushes. Never commits.
    """
    digest = await content_hash_async(text)
    max_number = await session.scalar(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
            DocumentVersion.document_id == document.id
        )
    )
    current = DocumentVersion(
        document_id=document.id,
        version_number=int(max_number or 0) + 1,
        content=text,
        content_hash=digest,
    )
    session.add(current)
    document.current_version = current.version_number
    document.content_hash = digest
    await session.flush()
    return current


def _add_prepared_chunks(
    session: AsyncSession,
    current: DocumentVersion,
    prepared_chunks: list[tuple[str, int, dict[str, object]]],
) -> None:
    """Add one chunk row per prepared ``(content, token_count, metadata)`` in order.

    Caller has proven the version has no chunks yet; rows are only added to the session (no flush).
    """
    for index, (content, token_count, metadata) in enumerate(prepared_chunks):
        session.add(
            DocumentChunk(
                document_version_id=current.id,
                chunk_index=index,
                content=content,
                content_hash=content_hash(content),
                token_count=token_count,
                metadata_json=metadata,
            )
        )


def _prepare_chunks(chunks: list[dict[str, object]]) -> list[tuple[str, int, dict[str, object]]]:
    """Convert parser chunk dicts to validated ``(content, token_count, metadata)`` tuples.

    Pure, run before any mutation so a malformed chunk raises ``KeyError``/``ValueError``/``TypeError``
    without side effects.
    """
    prepared_chunks = [
        (
            str(chunk["content"]),
            int(cast("int", chunk["token_count"])),
            dict(cast("dict[str, object]", chunk.get("metadata", {}))),
        )
        for chunk in chunks
    ]
    return prepared_chunks


async def save_extraction(
    session: AsyncSession,
    document_id: UUID,
    source_id: UUID,
    text: str,
    chunks: list[dict[str, object]],
    extraction_status: str,
    extraction_metadata: dict[str, object],
    warnings: list[str],
    parser: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
    expected_raw_uri: str,
    expected_mime_type: str,
) -> UUID | None:
    """Save parser output on its held parent, preserving original raw URI/MIME/fences.

    Prevalidate chunk conversion and metadata before mutation. Caller retains
    early admission/Source/Document/URI lifecycle locks; no earlier acquisition
    occurs after outbox. Immutable versions advance monotonically and chunks are
    added once; scoped ready events are new-row writes in the same transaction.
    Missing/mismatching input returns None before writes. Any later conversion,
    publication or consistency error aborts the complete attempt; no commit/I/O.
    """
    if type(text) is not str or extraction_status not in {
        "queued",
        "processing",
        "succeeded",
        "failed",
        "ready",
        "needs_ocr",
    }:
        raise ValueError("Invalid parser text or extraction status")
    prepared_chunks = _prepare_chunks(chunks)
    prepared_metadata, prepared_warnings = dict(extraction_metadata), list(warnings)
    document = await _extraction_document(
        session,
        document_id,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
        expected_raw_uri=expected_raw_uri,
        expected_mime_type=expected_mime_type,
    )
    if document is None:
        return None
    prepared_document_metadata = {
        **dict(document.metadata_json or {}),
        "extraction": prepared_metadata,
        "warnings": prepared_warnings,
        "parser": parser,
    }
    current = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == document.current_version,
        )
    )
    if current is None:
        raise RuntimeError("Current document version is missing")
    if current.content != text:
        current = await _append_text_version(session, document, text)
    document.extraction_status = extraction_status
    document.metadata_json = prepared_document_metadata
    existing = await session.scalar(
        select(DocumentChunk.id).where(DocumentChunk.document_version_id == current.id).limit(1)
    )
    if existing is None:
        _add_prepared_chunks(session, current, prepared_chunks)
    if extraction_status == "succeeded" and chunks:
        try:
            await _publish_extraction_ready(
                session,
                document,
                current,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence,
                source_fence=source_fence,
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError("extraction_ready_publication_conflict") from exc
    await session.flush()
    return document.id


async def read_extraction_input(
    session: AsyncSession,
    version_id: UUID,
    allowed_chunk_ids: list[UUID] | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ExtractionInput | None:
    """Return chunks for the active source's ready current version within extraction bounds.

    Raise ExtractionInputLimitError for empty, oversized or over-count input;
    unrelated selection validation remains ValueError. No partial input is returned.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    statement = (
        select(
            Document.id,
            Document.source_id,
            Source.generation,
            Source.local_only,
            DocumentVersion.id,
            DocumentVersion.observed_at,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentVersion.id == version_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            *_document_scope(scope),
        )
    )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    document_id, source_id, source_generation, local_only, actual_version_id, observed_at = row
    chunks_query = select(DocumentChunk.id, DocumentChunk.content).where(
        DocumentChunk.document_version_id == actual_version_id
    )
    if allowed_chunk_ids is not None:
        if (
            not allowed_chunk_ids
            or len(allowed_chunk_ids) > EXTRACTION_CHUNK_LIMIT
            or len(set(allowed_chunk_ids)) != len(allowed_chunk_ids)
        ):
            raise ValueError("Extraction chunk IDs must be unique and bounded")
        chunks_query = chunks_query.where(DocumentChunk.id.in_(allowed_chunk_ids))
    stats = await session.execute(
        select(
            func.count(DocumentChunk.id),
            func.coalesce(func.sum(func.octet_length(DocumentChunk.content)), 0),
        )
        .where(DocumentChunk.document_version_id == actual_version_id)
        .where(DocumentChunk.id.in_(allowed_chunk_ids) if allowed_chunk_ids is not None else true())
    )
    chunk_count, byte_count = cast("tuple[int, int]", tuple(stats.one()))
    if (
        not chunk_count
        or chunk_count > EXTRACTION_CHUNK_LIMIT
        or byte_count > EXTRACTION_INPUT_BYTES
    ):
        raise ExtractionInputLimitError("Extraction input exceeds its chunk or byte limit")
    chunks = list((await session.execute(chunks_query.order_by(DocumentChunk.chunk_index))).all())
    if allowed_chunk_ids is not None and {identifier for identifier, _ in chunks} != set(
        allowed_chunk_ids
    ):
        return None
    return ExtractionInput(
        document_id=document_id,
        document_version_id=actual_version_id,
        source_id=source_id,
        source_generation=source_generation,
        local_only=local_only,
        observed_at=observed_at,
        chunks=tuple(
            ExtractionChunk(id=identifier, content=content) for identifier, content in chunks
        ),
    )


async def get_first_chunk_id(
    session: AsyncSession,
    version_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Return the first chunk ID of a version in the admitted workspace without reading chunk content.

    Members are denied before the query; a version of another workspace or an ineligible Source
    returns None. Deterministic provider mappers anchor evidence on the title chunk; unlike
    ``read_extraction_input`` this has no chunk-count or byte ceiling, so an oversized
    body cannot make a record unmappable.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return await session.scalar(
        select(DocumentChunk.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentChunk.document_version_id == version_id, *_document_scope(scope))
        .order_by(DocumentChunk.chunk_index)
        .limit(1)
    )


async def read_extraction_evidence_refs(
    session: AsyncSession,
    *,
    document_id: UUID,
    document_version_id: UUID,
    source_id: UUID,
    source_generation: int,
    chunk_ids: list[UUID],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ExtractionEvidenceRef] | None:
    """Validate a bounded set of current extraction chunks and return detached evidence refs."""
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not chunk_ids or len(chunk_ids) > 150 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Extraction membership evidence must be nonempty and bounded")
    rows = (
        await session.execute(
            select(
                Document.id,
                DocumentVersion.id,
                Document.source_id,
                Source.generation,
                DocumentChunk.id,
            )
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.id == document_id,
                *_document_scope(scope),
                Document.source_id == source_id,
                DocumentVersion.id == document_version_id,
                Document.current_version == DocumentVersion.version_number,
                Document.extraction_status.in_(("ready", "succeeded")),
                Source.status == "active",
                Source.generation == source_generation,
                DocumentChunk.id.in_(chunk_ids),
            )
            .order_by(DocumentChunk.id)
        )
    ).all()
    if len(rows) != len(chunk_ids):
        return None
    return [
        ExtractionEvidenceRef(
            document_id=row[0],
            document_version_id=row[1],
            source_id=row[2],
            source_generation=row[3],
            chunk_id=row[4],
        )
        for row in rows
    ]
