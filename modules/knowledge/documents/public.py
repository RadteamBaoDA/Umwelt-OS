import base64
import binascii
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    delete,
    desc,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from core.chunking import chunk_text
from core.events import DomainEvent
from core.heavy_work import to_thread_joined
from core.pagination import decode_cursor, encode_cursor
from core.realtime import ReplayDraft, commit_with_replay, make_knowledge_change
from core.tools.schemas import ToolDestination, ToolOutputFence
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupEvidenceReference,
    DocumentCleanupOperation,
    DocumentInteraction,
    DocumentVersion,
    NormalizedDocumentIdentity,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    PROVIDER_IDS,
    DocumentCreate,
    DocumentExportFence,
    DocumentExportFenceValidation,
    DocumentExportPage,
    DocumentExportProvenance,
    DocumentExportRead,
    DocumentPatch,
    DocumentVersionExportRead,
    EvidenceReferenceRead,
    GadgetDocumentInteractionPatch,
    GadgetDocumentInteractionRead,
    GadgetDocumentProjectionList,
    GadgetDocumentProjectionRead,
    GadgetDocumentSelectionFence,
    GadgetHighlightProjectionPage,
    GadgetProviderMetadataRead,
    GadgetTelegramMediaRead,
    GadgetTelegramRecordRead,
    NormalizedDocumentInput,
    NormalizedDocumentResult,
    ObservationExportEvidenceCandidate,
    ObservationExportEvidenceRead,
    ProviderDocumentSnapshotList,
    ProviderDocumentSnapshotRead,
    ProviderRecordMetadata,
    TimelineExportEvidenceCandidate,
    TimelineExportEvidenceRead,
)
from modules.sources import public as sources
from modules.sources.models import Source
from modules.sources.schemas import SourceExportFence

if TYPE_CHECKING:
    from modules.connectors.public import ProviderScopeSnapshot


async def observability_quality_summary(session: AsyncSession) -> dict[str, int]:
    """Return document-owned aggregate counts without exposing document metadata."""
    document_count = int(await session.scalar(select(func.count()).select_from(Document)) or 0)
    orphan_chunks = int(await session.scalar(select(func.count()).select_from(DocumentChunk).outerjoin(
        DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id,
    ).where(DocumentVersion.id.is_(None))) or 0)
    return {"document_count": document_count, "orphan_chunks": orphan_chunks}

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "EvidenceReferenceRead",
    "ObservationExportEvidenceCandidate",
    "ProviderRecordMetadata",
    "TimelineExportEvidenceCandidate",
]

EXTRACTION_CHUNK_LIMIT = 100
EXTRACTION_INPUT_BYTES = 64_000
EXPORT_PAGE_MAX_BYTES = 16_777_216


@dataclass(frozen=True)
class DocumentCleanupEvidenceIdentity:
    """Carry one captured immutable version or chunk identity after hard deletion."""

    document_version_id: UUID
    chunk_id: UUID | None
    reference_kind: str


@dataclass(frozen=True)
class DocumentCleanupEvidenceScope:
    """Return a bounded detached page of one deleted document's evidence identities."""

    operation_id: UUID
    source_id: UUID
    document_id: UUID
    references: tuple[DocumentCleanupEvidenceIdentity, ...]
    next_cursor: UUID | None


@dataclass(frozen=True)
class SourceCleanupProgress:
    """Expose bounded Documents-owned aggregate counts without receipt identities.

    ``child_count``/``pending_count``/``failed_count`` describe receipts linked to the asking
    Source operation. ``historical_*`` describe every other retained same-source receipt (NULL or
    older linkage). ``all_required_complete`` requires both populations to be fully complete.
    """

    child_count: int
    capture_complete: bool
    pending_count: int
    failed_count: int
    all_required_complete: bool
    pending_owner_codes: tuple[str, ...]
    historical_count: int = 0
    historical_pending_count: int = 0
    historical_failed_count: int = 0
    active_copy_work: bool = False


async def pending_document_memory_cleanup_ids(
    session: AsyncSession, *, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return a bounded keyset page of captured receipts ready for their Memory stage.

    The worker uses stable receipt-derived event IDs, so repeated reconciliation is idempotent.
    Chat must be terminal-success before Memory is admitted; unavailable scopes are not retried.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Memory cleanup reconciliation page size must be between 1 and 100")
    return tuple((await session.scalars(
        select(DocumentCleanupOperation.id)
        .where(
            DocumentCleanupOperation.evidence_scope_status == "captured",
            DocumentCleanupOperation.chat_status == "succeeded",
            DocumentCleanupOperation.memory_status == "queued",
        )
        .order_by(DocumentCleanupOperation.id)
        .limit(limit)
    )).all())


async def pending_document_agent_cleanup_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return cleanup receipts whose Agent owner stage needs event reconciliation."""
    if not 1 <= limit <= 100:
        raise ValueError("Agent cleanup reconciliation page size must be between 1 and 100")
    statement = select(DocumentCleanupOperation.id).where(
        DocumentCleanupOperation.agent_status.in_({"queued", "running"}),
    )
    if after is not None:
        statement = statement.where(DocumentCleanupOperation.id > after)
    return tuple((await session.scalars(
        statement.order_by(DocumentCleanupOperation.id).limit(limit)
    )).all())


async def pending_document_copied_stage_cleanup_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Return receipts whose materialization or saved-brief stage still needs event reconciliation.

    Terminal failures (unavailable lineage) are excluded so legacy gaps never hot-loop; retryable
    failures are rescheduled by their own durable event, not by this partial-index scan.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Copied-stage reconciliation page size must be between 1 and 100")
    statement = select(DocumentCleanupOperation.id).where(or_(
        DocumentCleanupOperation.materialization_status.in_({"queued", "running"}),
        DocumentCleanupOperation.brief_status.in_({"queued", "running"}),
    ))
    if after is not None:
        statement = statement.where(DocumentCleanupOperation.id > after)
    return tuple((await session.scalars(
        statement.order_by(DocumentCleanupOperation.id).limit(limit)
    )).all())


def _encode_document_export_cursor(
    owner_id: int, record_kind: str, snapshot_at: datetime, position_at: datetime, position_id: UUID,
) -> str:
    """Encode a canonical owner/kind/cutoff-bound keyset cursor for document exports."""
    payload = {
        "v": 1, "owner": owner_id, "kind": record_kind,
        "snapshot": snapshot_at.astimezone(UTC).isoformat(),
        "at": position_at.astimezone(UTC).isoformat(), "id": str(position_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_document_export_cursor(
    cursor: str, owner_id: int, record_kind: str,
) -> tuple[datetime, datetime, UUID]:
    """Decode a canonical bounded cursor and reject cross-owner or cross-domain replay."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid document export cursor")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"v", "owner", "kind", "snapshot", "at", "id"}:
            raise ValueError("Invalid document export cursor")
        if payload["v"] != 1 or payload["owner"] != owner_id or payload["kind"] != record_kind:
            raise ValueError("Document export cursor belongs to another owner or record kind")
        snapshot_at = datetime.fromisoformat(payload["snapshot"])
        position_at = datetime.fromisoformat(payload["at"])
        if any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, position_at)):
            raise ValueError("Document export cursor timestamps must be timezone-aware")
        snapshot_at, position_at = snapshot_at.astimezone(UTC), position_at.astimezone(UTC)
        if snapshot_at > datetime.now(UTC):
            raise ValueError("Document export cursor cutoff cannot be in the future")
        position_id = UUID(payload["id"])
        if _encode_document_export_cursor(owner_id, record_kind, snapshot_at, position_at, position_id) != cursor:
            raise ValueError("Document export cursor is not canonical")
        return snapshot_at, position_at, position_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("Invalid document export cursor") from exc


def _safe_export_url(value: str | None) -> str | None:
    """Drop credentials, query strings, fragments, and non-web URLs from exported provenance."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return None


def _export_payload_bytes(items: Sequence[BaseModel]) -> int:
    """Measure the exact JSON array bytes of typed records before returning a page."""
    return len(json.dumps(
        [item.model_dump(mode="json") for item in items],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


def _export_item_bytes(item: BaseModel) -> int:
    """Measure one serialized export record so page admission stays linear in payload size."""
    return len(json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def _require_document_export_owner(session: AsyncSession, owner_id: int) -> None:
    """Require the live singleton owner before projecting source-owned documents."""
    if owner_id != 1 or await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        raise PermissionError("Document export requires the current owner")


def _document_export_scope(snapshot_at: datetime) -> tuple[ColumnElement[bool], ...]:
    """Select retained documents that existed and were unchanged at the page cutoff."""
    return Document.created_at <= snapshot_at, Document.updated_at <= snapshot_at


async def _document_export_count(
    session: AsyncSession, record_kind: str, snapshot_at: datetime,
) -> int:
    """Count owner-visible rows at the fixed cutoff so callers can detect export drift."""
    if record_kind == "documents":
        statement = select(func.count()).select_from(Document).join(Source, Source.id == Document.source_id)
        statement = statement.where(
            *_document_export_scope(snapshot_at),
            Document.source_id.in_(sources.export_eligible_source_ids()),
        )
    else:
        statement = (
            select(func.count()).select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                *_document_export_scope(snapshot_at), DocumentVersion.created_at <= snapshot_at,
                Document.source_id.in_(sources.export_eligible_source_ids()),
            )
        )
    return int(await session.scalar(statement) or 0)


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
) -> DocumentExportPage:
    """Return safe document headers or full retained revisions through a bounded stable page.

    The current owner must be authenticated by the calling operation and is rechecked here.
    Archived and paused sources remain exportable while their document rows are retained. A
    fixed cutoff, exact cursor binding, current source generation, owner count, and final row
    fences let the caller reject changes or deletion before publishing; no path, raw file,
    arbitrary metadata, provider payload, or content digest is included in exported items.
    """
    if record_kind not in {"documents", "versions"} or not 1 <= limit <= 100:
        raise ValueError("Document export kind or page limit is invalid")
    await _require_document_export_owner(session, owner_id)
    if cursor is None:
        snapshot_at = datetime.now(UTC)
        position = None
    else:
        snapshot_at, position_at, position_id = _decode_document_export_cursor(cursor, owner_id, record_kind)
        position = (position_at, position_id)
    snapshot_count = await _document_export_count(session, record_kind, snapshot_at)
    items: list[DocumentExportRead | DocumentVersionExportRead] = []
    fences: list[DocumentExportFence] = []
    has_more = False
    payload_bytes = 2

    if record_kind == "documents":
        statement = (
            select(
                Document.id.label("document_id"), Document.source_id.label("source_id"),
                Document.created_at.label("document_created_at"), Document.updated_at.label("document_updated_at"),
                Document.current_version.label("document_current_version"), Document.title.label("document_title"),
                Document.content_type.label("document_content_type"), Document.mime_type.label("document_mime_type"),
                Document.canonical_url.label("document_canonical_url"), Source.status.label("source_status"),
                Source.generation.label("source_generation"),
                DocumentVersion.version_number.label("current_version_number"),
                NormalizedVersionProvenance.source_generation.label("accepted_generation"),
            )
            .join(Source, Source.id == Document.source_id)
            .outerjoin(DocumentVersion, and_(
                DocumentVersion.document_id == Document.id,
                DocumentVersion.version_number == Document.current_version,
            ))
            .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
            .where(
                *_document_export_scope(snapshot_at),
                Document.source_id.in_(sources.export_eligible_source_ids()),
            )
        )
        if position is not None:
            statement = statement.where(tuple_(Document.created_at, Document.id) > position)
        rows = (await session.execute(
            statement.order_by(Document.created_at, Document.id).limit(limit + 1)
        )).all()
        for raw_row in rows:
            if len(items) == limit:
                has_more = True
                break
            row = raw_row._mapping
            if row["current_version_number"] is None:
                raise ValueError("A retained document has no current immutable version")
            item = DocumentExportRead(
                id=row["document_id"], source_id=row["source_id"], source_status=row["source_status"],
                current_source_generation=row["source_generation"], current_version=row["document_current_version"],
                current_version_accepted_generation=row["accepted_generation"],
                title=row["document_title"], content_type=row["document_content_type"],
                mime_type=row["document_mime_type"], canonical_url=_safe_export_url(row["document_canonical_url"]),
                created_at=row["document_created_at"], updated_at=row["document_updated_at"],
            )
            item_bytes = _export_item_bytes(item)
            proposed_bytes = payload_bytes + item_bytes + (1 if items else 0)
            if proposed_bytes > EXPORT_PAGE_MAX_BYTES:
                if not items:
                    raise ValueError("A document export record exceeds the page byte budget")
                has_more = True
                break
            items.append(item)
            payload_bytes = proposed_bytes
            fences.append(DocumentExportFence(
                document_id=row["document_id"], document_created_at=row["document_created_at"],
                document_updated_at=row["document_updated_at"], document_current_version=row["document_current_version"],
                source_id=row["source_id"], source_status=row["source_status"],
                current_source_generation=row["source_generation"],
            ))
        if len(rows) > len(items):
            has_more = True
        next_cursor = (
            _encode_document_export_cursor(owner_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id)
            if has_more and items else None
        )
    else:
        version_statement = (
            select(
                Document.id.label("document_id"), Document.source_id.label("source_id"),
                Document.created_at.label("document_created_at"), Document.updated_at.label("document_updated_at"),
                Document.current_version.label("document_current_version"),
                Source.status.label("source_status"), Source.generation.label("source_generation"),
                DocumentVersion.id.label("version_id"), DocumentVersion.version_number.label("version_number"),
                DocumentVersion.content.label("version_content"), DocumentVersion.content_hash.label("version_content_hash"),
                DocumentVersion.observed_at.label("version_observed_at"),
                DocumentVersion.created_at.label("version_created_at"),
                NormalizedVersionProvenance.provider_id.label("provider_id"),
                NormalizedVersionProvenance.provider_version.label("provider_version"),
                NormalizedVersionProvenance.normalization_version.label("normalization_version"),
                NormalizedVersionProvenance.source_generation.label("accepted_source_generation"),
                NormalizedVersionProvenance.observed_at.label("provenance_observed_at"),
                NormalizedVersionProvenance.received_at.label("received_at"),
                NormalizedVersionProvenance.collected_at.label("collected_at"),
                NormalizedVersionProvenance.selection_observed_at.label("selection_observed_at"),
                NormalizedVersionProvenance.title.label("provenance_title"),
                NormalizedVersionProvenance.canonical_url.label("provenance_canonical_url"),
                NormalizedVersionProvenance.published_at.label("provenance_published_at"),
                NormalizedVersionProvenance.content_type.label("provenance_content_type"),
            )
            .join(Source, Source.id == Document.source_id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
            .where(
                *_document_export_scope(snapshot_at), DocumentVersion.created_at <= snapshot_at,
                Document.source_id.in_(sources.export_eligible_source_ids()),
            )
        )
        if position is not None:
            version_statement = version_statement.where(tuple_(DocumentVersion.created_at, DocumentVersion.id) > position)
        result = await session.stream(
            version_statement.order_by(DocumentVersion.created_at, DocumentVersion.id)
            .limit(limit + 1).execution_options(yield_per=10)
        )
        try:
            async for version_raw_row in result.mappings():
                if len(items) == limit:
                    has_more = True
                    break
                version_row = version_raw_row
                safe_provenance = DocumentExportProvenance(
                    provider_id=version_row["provider_id"], provider_version=version_row["provider_version"],
                    normalization_version=version_row["normalization_version"],
                    accepted_source_generation=version_row["accepted_source_generation"],
                    observed_at=version_row["provenance_observed_at"], received_at=version_row["received_at"],
                    collected_at=version_row["collected_at"], selection_observed_at=version_row["selection_observed_at"],
                    title=version_row["provenance_title"], canonical_url=_safe_export_url(version_row["provenance_canonical_url"]),
                    published_at=version_row["provenance_published_at"], content_type=version_row["provenance_content_type"],
                ) if version_row["provider_id"] is not None else None
                version_item = DocumentVersionExportRead(
                    id=version_row["version_id"], document_id=version_row["document_id"], source_id=version_row["source_id"],
                    source_status=version_row["source_status"], current_source_generation=version_row["source_generation"],
                    version_number=version_row["version_number"],
                    is_current_version=version_row["version_number"] == version_row["document_current_version"],
                    content=version_row["version_content"], observed_at=version_row["version_observed_at"],
                    created_at=version_row["version_created_at"], provenance=safe_provenance,
                )
                item_bytes = _export_item_bytes(version_item)
                proposed_bytes = payload_bytes + item_bytes + (1 if items else 0)
                if proposed_bytes > EXPORT_PAGE_MAX_BYTES:
                    if not items:
                        raise ValueError("A document version exceeds the page byte budget")
                    has_more = True
                    break
                items.append(version_item)
                payload_bytes = proposed_bytes
                fences.append(DocumentExportFence(
                    document_id=version_row["document_id"], document_created_at=version_row["document_created_at"],
                    document_updated_at=version_row["document_updated_at"], document_current_version=version_row["document_current_version"],
                    source_id=version_row["source_id"], source_status=version_row["source_status"],
                    current_source_generation=version_row["source_generation"], version_id=version_row["version_id"],
                    version_number=version_row["version_number"], version_created_at=version_row["version_created_at"],
                    version_content_digest=version_row["version_content_hash"],
                ))
        finally:
            await result.close()
        next_cursor = (
            _encode_document_export_cursor(owner_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id)
            if has_more and items else None
        )

    if len(items) != len(fences):
        raise RuntimeError("Document export page lost a record fence")
    return DocumentExportPage(
        owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=_export_payload_bytes(items), max_payload_bytes=EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor, available=True, omission_reason=None,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: Sequence[DocumentExportFence],
) -> DocumentExportFenceValidation:
    """Recheck bounded document identity, source generation, deletion, and count before publication."""
    if record_kind not in {"documents", "versions"} or not 0 <= expected_snapshot_count <= 2**63 - 1:
        raise ValueError("Document export revalidation input is invalid")
    if len(fences) > 100:
        raise ValueError("Document export revalidation is limited to 100 records")
    if owner_id != 1 or await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        return DocumentExportFenceValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    source_generations: dict[UUID, int] = {}
    for fence in fences:
        previous = source_generations.setdefault(fence.source_id, fence.current_source_generation)
        if previous != fence.current_source_generation:
            observed_count = await _document_export_count(session, record_kind, snapshot_at)
            return DocumentExportFenceValidation(
                valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count,
            )
    source_fences = [
        SourceExportFence(source_id=source_id, generation=generation)
        for source_id, generation in source_generations.items()
    ]
    eligible_source_ids = set(await sources.filter_export_eligible_sources(session, source_fences))
    if len(eligible_source_ids) != len(source_fences):
        observed_count = await _document_export_count(session, record_kind, snapshot_at)
        return DocumentExportFenceValidation(
            valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count,
        )
    observed_count = await _document_export_count(session, record_kind, snapshot_at)
    if observed_count != expected_snapshot_count:
        return DocumentExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed_count)
    for fence in fences:
        row = (await session.execute(
            select(Document.created_at, Document.updated_at, Document.current_version,
                   Source.id.label("source_id"), Source.status, Source.generation)
            .join(Source, Source.id == Document.source_id)
            .where(Document.id == fence.document_id)
        )).one_or_none()
        if row is None or (
            row.created_at != fence.document_created_at
            or row.updated_at != fence.document_updated_at
            or row.source_id != fence.source_id
            or fence.document_current_version is not None and row.current_version != fence.document_current_version
        ):
            return DocumentExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed_count)
        if row.status != fence.source_status or row.generation != fence.current_source_generation:
            return DocumentExportFenceValidation(valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count)
        if record_kind == "versions":
            if fence.version_id is None or fence.version_number is None or fence.version_created_at is None:
                raise ValueError("Version export fence is missing its immutable revision identity")
            version = (await session.execute(
                select(DocumentVersion.version_number, DocumentVersion.created_at, DocumentVersion.content_hash)
                .where(DocumentVersion.id == fence.version_id, DocumentVersion.document_id == fence.document_id)
            )).one_or_none()
            if version is None:
                return DocumentExportFenceValidation(valid=False, reason="evidence_unavailable", observed_snapshot_count=observed_count)
            if (version.version_number != fence.version_number or version.created_at != fence.version_created_at
                    or version.content_hash != fence.version_content_digest):
                return DocumentExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed_count)
        elif fence.version_id is not None:
            raise ValueError("Document header export fence cannot include a version identity")
    return DocumentExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed_count)


class ExtractionInputLimitError(ValueError):
    """Signal an immutable version outside extraction limits without exposing its content.

    Scheduling owners may persist blocked work while acknowledging canonical
    readiness. Existing ValueError handlers retain their validation behavior.
    """


@dataclass(frozen=True)
class ObservationEvidenceCandidate:
    """Identify one structured point's immutable document acceptance fences."""
    observation_id: UUID
    source_id: UUID
    source_generation: int
    provider: str
    provider_scope_discriminator: str
    external_id: str
    document_id: UUID
    document_version_id: UUID


async def current_observation_evidence_versions(
    session: AsyncSession,
    candidates: Sequence[ObservationEvidenceCandidate],
    current_scopes: dict[UUID, object],
) -> dict[UUID, int]:
    """Return current evidence revisions backed by matching document and provider fences.

    Documents owns version/provenance reads. The result contains accepted observation IDs
    paired with their current version numbers; foreign modules never receive Document ORM
    rows or provenance bodies.
    """
    if not candidates or len(candidates) > 256:
        return {}
    rows: Any = (await session.execute(
        select(
            Document.id, DocumentVersion.id, Source.id, Source.generation,
            DocumentVersion.version_number, Document.current_version,
            NormalizedVersionProvenance.provider_id,
            NormalizedVersionProvenance.source_generation,
            NormalizedVersionProvenance.provenance_json,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(DocumentVersion.id.in_({item.document_version_id for item in candidates}))
    )).all()
    by_identity: dict[tuple[UUID, UUID], list[tuple[UUID, int, int, int, str, int, dict[str, object]]]] = {}
    for (
            doc_id, version_id, source_id, source_generation, version_number,
            current_version, provider_id, accepted_generation, provenance,
    ) in rows:
        by_identity.setdefault((doc_id, version_id), []).append((
            source_id, source_generation, version_number, current_version,
            provider_id, accepted_generation, provenance,
        ))
    accepted: dict[UUID, int] = {}
    for candidate in candidates:
        current = current_scopes.get(candidate.source_id)
        if (
            current is None or getattr(current, "provider_id", None) != candidate.provider
            or getattr(current, "source_generation", None) != candidate.source_generation
            or getattr(current, "discriminator", None) != candidate.provider_scope_discriminator
        ):
            continue
        matches = by_identity.get((candidate.document_id, candidate.document_version_id), [])
        if len(matches) != 1:
            continue
        row = matches[0]
        (
            source_id, source_generation, version_number, current_version,
            provider_id, accepted_generation, provenance,
        ) = row
        if (
            source_id == candidate.source_id and source_generation == candidate.source_generation
            and version_number == current_version and provider_id == candidate.external_id
            and accepted_generation == candidate.source_generation
            and isinstance(provenance, dict)
            and provenance.get("provider_scope_discriminator") == candidate.provider_scope_discriminator
        ):
            accepted[candidate.observation_id] = version_number
    return accepted


async def export_observation_evidence(
    session: AsyncSession, candidate: ObservationExportEvidenceCandidate,
    current_scope: "ProviderScopeSnapshot",
) -> ObservationExportEvidenceRead | None:
    """Prove retained observation provenance without requiring its accepted source generation to be current.

    Documents validates the exact current document revision, source identity, provider record identity,
    accepted generation and non-secret scope digest. The separate current source generation is returned
    for final backup fencing; paused or archived lifecycle changes do not rewrite historical acceptance.
    Pending data purge and changed scope fail closed.
    """
    from modules.connectors.public import ProviderScopeSnapshot

    if (not isinstance(current_scope, ProviderScopeSnapshot)
            or current_scope.source_id != candidate.source_id
            or current_scope.provider_id != candidate.provider
            or current_scope.discriminator != candidate.provider_scope_discriminator):
        return None
    rows = (await session.execute(select(
        Source.id.label("source_id"), Source.status.label("source_status"),
        Source.generation.label("current_source_generation"), Document.id.label("document_id"),
        Document.source_id.label("document_source_id"), Document.current_version.label("current_version"),
        DocumentVersion.id.label("document_version_id"),
        DocumentVersion.version_number.label("document_version_number"),
        NormalizedVersionProvenance.source_generation.label("accepted_source_generation"),
        NormalizedVersionProvenance.provider_id.label("provider_id"),
        NormalizedVersionProvenance.provenance_json.label("provenance"),
    ).join(Document, Document.source_id == Source.id)
      .join(DocumentVersion, DocumentVersion.document_id == Document.id)
      .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
      .where(
          Source.id == candidate.source_id, Source.generation == current_scope.source_generation,
          Source.status.in_(("active", "paused", "archived")), Source.id.in_(sources.export_eligible_source_ids()),
          Document.id == candidate.document_id, DocumentVersion.id == candidate.document_version_id,
          Document.current_version == DocumentVersion.version_number,
          NormalizedVersionProvenance.source_generation == candidate.accepted_source_generation,
          NormalizedVersionProvenance.provider_id == candidate.external_id,
          NormalizedVersionProvenance.provenance_json["provider_scope_discriminator"].astext
          == current_scope.discriminator,
      ).limit(2))).all()
    matches = [row for row in rows if isinstance(row.provenance, dict)
               and row.provenance.get("provider_scope_discriminator") == current_scope.discriminator]
    if len(matches) != 1:
        return None
    row = matches[0]
    return ObservationExportEvidenceRead(
        observation_id=candidate.observation_id, source_id=row.source_id,
        current_source_generation=row.current_source_generation,
        accepted_source_generation=row.accepted_source_generation,
        document_id=row.document_id, document_version_id=row.document_version_id,
        document_version_number=row.document_version_number, provider=current_scope.provider_id,
        provider_scope_discriminator=current_scope.discriminator,
    )


async def export_timeline_evidence(
    session: AsyncSession, candidate: TimelineExportEvidenceCandidate,
) -> TimelineExportEvidenceRead | None:
    """Prove a retained exact chunk and accepted generation, while separately fencing its current source.

    Timeline facts may retain support from an earlier source generation after a source is paused or
    archived. The source owner's purge eligibility and fresh scalar projection keep the evidence usable
    only while its exact source/document/version/chunk and accepted provenance remain retained.
    """
    count = func.count(NormalizedVersionProvenance.id)
    minimum_generation = func.min(NormalizedVersionProvenance.source_generation)
    maximum_generation = func.max(NormalizedVersionProvenance.source_generation)
    row = (await session.execute(select(
        Source.id.label("source_id"), Source.status.label("source_status"),
        Source.generation.label("current_source_generation"), Document.id.label("document_id"),
        DocumentVersion.id.label("document_version_id"), DocumentChunk.id.label("chunk_id"),
        count.label("provenance_count"), minimum_generation.label("minimum_accepted_generation"),
        maximum_generation.label("maximum_accepted_generation"),
    ).join(Document, Document.source_id == Source.id)
      .join(DocumentVersion, DocumentVersion.document_id == Document.id)
      .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
      .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
      .where(
          Source.id == candidate.source_id,
          Source.status.in_(("active", "paused", "archived")),
          Source.id.in_(sources.export_eligible_source_ids()),
          Document.id == candidate.document_id,
          DocumentVersion.id == candidate.document_version_id,
          DocumentChunk.id == candidate.chunk_id,
      ).group_by(Source.id, Source.status, Source.generation, Document.id, DocumentVersion.id, DocumentChunk.id)
      .limit(1))).one_or_none()
    if (row is None or row.source_id != candidate.source_id or row.provenance_count < 1
            or row.minimum_accepted_generation != candidate.accepted_source_generation
            or row.maximum_accepted_generation != candidate.accepted_source_generation):
        return None
    return TimelineExportEvidenceRead(
        evidence_id=candidate.evidence_id, source_id=row.source_id,
        accepted_source_generation=candidate.accepted_source_generation,
        current_source_generation=row.current_source_generation,
        document_id=row.document_id, document_version_id=row.document_version_id, chunk_id=row.chunk_id,
    )


def _provider_snapshot(
    document: Document,
    version: DocumentVersion,
    source: Source,
    provenance: NormalizedVersionProvenance | None,
) -> ProviderDocumentSnapshotRead:
    """Project immutable version provenance, marking legacy mutable fallback clearly."""
    metadata = None
    if provenance is not None:
        raw_metadata = dict(provenance.provenance_json)
        raw_provider = raw_metadata.get("provider_record")
        if raw_provider is not None:
            metadata = ProviderRecordMetadata.model_validate(raw_provider)
        title = provenance.title
        canonical_url = provenance.canonical_url
        published_at = provenance.published_at
        provider_id = provenance.provider_id
        provider_version = provenance.provider_version
        observed_at = provenance.selection_observed_at
        received_at = provenance.received_at
        collected_at = provenance.collected_at
        snapshot = True
    else:
        title = document.title
        canonical_url = document.canonical_url
        published_at = document.published_at
        provider_id = document.external_id or ""
        provider_version = None
        observed_at = version.observed_at
        received_at = collected_at = None
        snapshot = False
    return ProviderDocumentSnapshotRead(
        document_id=document.id,
        document_version_id=version.id,
        version_number=version.version_number,
        source_id=source.id,
        source_status=source.status,
        provider_id=provider_id,
        provider_version=provider_version,
        title=title,
        canonical_url=canonical_url,
        published_at=published_at,
        observed_at=observed_at,
        received_at=received_at,
        collected_at=collected_at,
        excerpt=version.content[:1000],
        provider_metadata=metadata,
        metadata_is_version_snapshot=snapshot,
    )


def _encode_provider_cursor(created_at: datetime, document_id: UUID) -> str:
    """Encode the stable descending document creation keyset as bounded base64 JSON."""
    encoded = json.dumps(
        [created_at.astimezone(UTC).isoformat(), str(document_id)], separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_provider_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Strictly decode one timestamp/UUID keyset cursor without accepting junk."""
    if not cursor or len(cursor) > 1024:
        raise ValueError("Invalid provider snapshot cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 2 or not all(isinstance(x, str) for x in values):
            raise ValueError
        created_at = datetime.fromisoformat(values[0])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        document_id = UUID(values[1])
        if _encode_provider_cursor(created_at, document_id) != cursor:
            raise ValueError
        return created_at.astimezone(UTC), document_id
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid provider snapshot cursor") from exc


async def read_provider_snapshots(
    session: AsyncSession, version_ids: list[UUID]
) -> list[ProviderDocumentSnapshotRead]:
    """Read exact immutable provider versions for an already owner-authenticated route.

    This query is intentionally absent from agent, tool, and collector entry points;
    a future agent surface must apply P07 grants before calling a suitable owner API.
    Missing, deleted, archived, or duplicate versions fail as a whole request.
    """
    if not 1 <= len(version_ids) <= 100 or len(version_ids) != len(set(version_ids)):
        raise ValueError("version_ids must contain 1 to 100 unique values")
    rows = list((await session.execute(
        select(Document, DocumentVersion, Source, NormalizedVersionProvenance)
        .join(DocumentVersion, DocumentVersion.id.in_(version_ids))
        .join(Source, Source.id == Document.source_id)
        .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(Document.id == DocumentVersion.document_id, Source.provider.in_(PROVIDER_IDS))
    )).all())
    by_id = {version.id: _provider_snapshot(document, version, source, provenance)
             for document, version, source, provenance in rows
             if source.status in {"active", "paused"}}
    if len(by_id) != len(version_ids):
        raise ValueError("One or more provider document versions are unavailable")
    return [by_id[version_id] for version_id in version_ids]


async def list_provider_snapshots(
    session: AsyncSession,
    *,
    source_ids: list[UUID],
    channel_ids: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> ProviderDocumentSnapshotList:
    """List current exact versions for an already owner-authenticated route.

    Pagination is a bounded created-at/ID keyset; only typed source/channel scope
    is read, and no agent/tool authorization is implied by this owner projection.
    """
    if not 1 <= len(source_ids) <= 100 or len(source_ids) != len(set(source_ids)):
        raise ValueError("source_ids must contain 1 to 100 unique values")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if channel_ids is not None and (
        len(channel_ids) > 100 or len(channel_ids) != len(set(channel_ids))
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("channel_ids must contain at most 100 unique values")
    statement = (
        select(Document, DocumentVersion, Source, NormalizedVersionProvenance)
        .join(Source, Source.id == Document.source_id)
        .join(DocumentVersion, (DocumentVersion.document_id == Document.id)
              & (DocumentVersion.version_number == Document.current_version))
        .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(
            Document.source_id.in_(source_ids),
            Source.status.in_(("active", "paused")),
            Source.provider.in_(PROVIDER_IDS),
        )
    )
    if cursor is not None:
        created_at, document_id = _decode_provider_cursor(cursor)
        statement = statement.where(tuple_(Document.created_at, Document.id) < tuple_(created_at, document_id))
    if channel_ids is not None:
        statement = statement.where(
            NormalizedVersionProvenance.provenance_json["provider_record"]["telegram"]["channel_id"].astext.in_(channel_ids)
        )
    rows = list((await session.execute(
        statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1)
    )).all())
    page = rows[:limit]
    next_cursor = _encode_provider_cursor(page[-1][0].created_at, page[-1][0].id) if len(rows) > limit and page else None
    return ProviderDocumentSnapshotList(
        items=[_provider_snapshot(document, version, source, provenance)
               for document, version, source, provenance in page],
        next_cursor=next_cursor,
    )


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


@dataclass(frozen=True)
class ReviewVersionFence:
    """Snapshot document/source identity and current source generation for review."""
    document_id: UUID
    source_id: UUID
    current_source_generation: int
    source_name: str
    version_number: int


@dataclass(frozen=True)
class NewsChunkProjection:
    """Expose bounded immutable chunk identity and text to the News owner."""
    id: UUID
    index: int
    content: str


@dataclass(frozen=True)
class NewsDocumentProjection:
    """Carry a detached current-version snapshot for owner-authorized News reads."""
    document_id: UUID
    document_version_id: UUID
    version_number: int
    source_id: UUID
    current_source_generation: int
    source_name: str
    source_type: str
    provider: str | None
    local_only: bool
    canonical_url: str | None
    content_hash: str
    provider_item_id: str | None
    scope_discriminator: str | None
    title: str
    published_at: datetime | None
    observed_at: datetime
    created_at: datetime
    chunks: tuple[NewsChunkProjection, ...]
    metadata_is_version_snapshot: bool
    accepted_record_hash: str | None
    normalization_version: int | None
    chunk_count: int
    chunks_truncated: bool
    provider_metadata: ProviderRecordMetadata | None


@dataclass(frozen=True)
class NewsProjectionStatus:
    """Expose bounded non-sensitive reasons that selected current evidence is incomplete."""
    incomplete_reasons: tuple[str, ...]


async def get_news_document_projection(
    session: AsyncSession, document_id: UUID, *, expected_source_generation: int | None = None,
) -> NewsDocumentProjection | None:
    """Return a current, ready document projection only under its active source generation.

    The caller must first authorize the source through the Sources public contract.
    This read excludes deleted, paused, replaced, or incomplete versions and caps
    chunks at 100. Legacy/manual documents remain readable with explicitly
    non-snapshot metadata provenance rather than inferred historical metadata.
    """
    components = await _current_document_components(
        session, document_id, expected_source_generation=expected_source_generation,
    )
    if components is None:
        return None
    document, version, source, provenance = components
    chunk_rows = list((await session.execute(
        select(DocumentChunk.id, DocumentChunk.chunk_index, DocumentChunk.content)
        .where(DocumentChunk.document_version_id == version.id)
        .order_by(DocumentChunk.chunk_index, DocumentChunk.id).limit(101)
    )).all())
    if not chunk_rows:
        return None
    chunk_count = len(chunk_rows) if len(chunk_rows) <= 100 else int(await session.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
    ) or 0)
    chunks_truncated = len(chunk_rows) > 100
    chunk_rows = chunk_rows[:100]
    return NewsDocumentProjection(
        document_id=document.id, document_version_id=version.id, version_number=version.version_number,
        source_id=source.id, current_source_generation=source.generation, source_name=source.name,
        source_type=source.type, provider=source.provider, local_only=source.local_only,
        canonical_url=provenance.canonical_url if provenance else document.canonical_url,
        content_hash=version.content_hash,
        provider_item_id=(document.external_id[:512] if document.external_id else None),
        scope_discriminator=(cast("str | None", provenance.provenance_json.get("provider_scope_discriminator")) if provenance else None),
        title=provenance.title if provenance else document.title,
        published_at=provenance.published_at if provenance else document.published_at,
        observed_at=provenance.selection_observed_at if provenance else (document.observed_at or version.observed_at),
        created_at=version.created_at,
        chunks=tuple(NewsChunkProjection(id=identifier, index=index, content=content[:20_000]) for identifier, index, content in chunk_rows),
        metadata_is_version_snapshot=provenance is not None,
        accepted_record_hash=provenance.accepted_record_hash if provenance else None,
        normalization_version=provenance.normalization_version if provenance else None,
        chunk_count=chunk_count, chunks_truncated=chunks_truncated,
        provider_metadata=(
            ProviderRecordMetadata.model_validate(provenance.provenance_json["provider_record"])
            if provenance and provenance.provenance_json.get("provider_record") is not None else None
        ),
    )


async def _current_document_components(
    session: AsyncSession, document_id: UUID, *, expected_source_generation: int | None = None,
) -> tuple[Document, DocumentVersion, Source, NormalizedVersionProvenance | None] | None:
    """Resolve one active current revision and verify its accepted provider scope without reading chunks.

    The Documents owner uses this shared gate for content projections and exact selections so a
    normalized record cannot inherit a new source generation or mutable provider configuration.
    """
    row = (await session.execute(
        select(Document, DocumentVersion, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            *([Source.generation == expected_source_generation] if expected_source_generation is not None else []),
        )
    )).one_or_none()
    if row is None:
        return None
    document, version, source = row
    provenance_rows = list((await session.scalars(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_version_id == version.id,
        ).order_by(
            NormalizedVersionProvenance.accepted_record_hash,
            NormalizedVersionProvenance.normalization_version,
        ).limit(2)
    )).all())
    if provenance_rows and (
        len(provenance_rows) != 1 or provenance_rows[0].source_generation != source.generation
    ):
        # Normalized data cannot fall back to mutable legacy metadata when its
        # accepted generation is stale or its immutable identity is ambiguous.
        return None
    if len(provenance_rows) > 1:
        return None
    if provenance_rows:
        provenance = provenance_rows[0]
        if source.type in {"rss", "web", "api"}:
            from modules.connectors import public as connectors

            current_scope = await connectors.get_current_provider_scope(
                session, source.id, source.generation,
            )
            accepted_scope = provenance.provenance_json.get("provider_scope_discriminator")
            if (
                current_scope is None or not isinstance(accepted_scope, str)
                or len(accepted_scope) != 64 or accepted_scope != current_scope.discriminator
            ):
                return None
    else:
        if source.type in {"rss", "web", "api"}:
            # Provider documents without immutable acceptance provenance cannot
            # establish their current scope and must not use mutable legacy fields.
            return None
        provenance = None
    return document, version, source, provenance


async def validate_gadget_document_selection_fences(
    session: AsyncSession, fences: tuple[GadgetDocumentSelectionFence, ...], *,
    lock_rows: bool = True, max_documents: int = 32,
) -> bool:
    """Revalidate exact selected versions and provider policy before content or remote use.

    Selection count, source count, and lock order are bounded. Source rows are share-locked in UUID
    order before document rows; accepted provenance is then checked against the live provider scope.
    Locks remain held by the caller's transaction until its next commit or rollback.
    """
    if (
        not fences or len(fences) > max_documents or max_documents > 100
        or len({item.document_id for item in fences}) != len(fences)
        or len({item.source_id for item in fences}) > 32
    ):
        raise ValueError("Selection fences exceed their bounded unique-document or source limit")
    source_ids = sorted({item.source_id for item in fences}, key=str)
    document_ids = sorted({item.document_id for item in fences}, key=str)
    if lock_rows:
        # Scope/configuration writers lock Source before changing documents; keep the same order here.
        locked_sources = (await session.scalars(
            select(Source).where(Source.id.in_(source_ids)).order_by(Source.id).with_for_update(read=True)
        )).all()
        if len(locked_sources) != len(source_ids):
            return False
        locked_documents = (await session.scalars(
            select(Document).where(Document.id.in_(document_ids)).order_by(Document.id).with_for_update(read=True)
        )).all()
        if len(locked_documents) != len(document_ids):
            return False
    for fence in fences:
        components = await _current_document_components(
            session, fence.document_id, expected_source_generation=fence.source_generation,
        )
        if components is None:
            return False
        _document, version, source, provenance = components
        accepted_scope = provenance.provenance_json.get("provider_scope_discriminator") if provenance else None
        if (
            version.id != fence.document_version_id
            or source.id != fence.source_id
            or source.generation != fence.source_generation
            or source.type != fence.source_type
            or source.provider != fence.provider
            or source.local_only != fence.local_only
            or accepted_scope != fence.scope_discriminator
        ):
            return False
    return True


async def news_retained_observation_allowed(
    session: AsyncSession, *, document_id: UUID, source_id: UUID,
    expected_source_generation: int,
) -> bool:
    """Confirm a retained News count still belongs to the current active source scope.

    This boolean fence deliberately does not return historical or current text and
    does not require the observed historical version to remain current. It checks
    the document's present ready revision, active source generation, and current
    provider scope without loading chunks or exposing metadata to News.
    """
    rows: list[Any] = list((await session.execute(
        select(
            Source.type, Source.generation,
            NormalizedVersionProvenance.source_generation,
            NormalizedVersionProvenance.provenance_json,
        )
        .join(Document, Document.source_id == Source.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .outerjoin(
            NormalizedVersionProvenance,
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
        )
        .where(
            Document.id == document_id, Document.source_id == source_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.generation == expected_source_generation,
        )
        .order_by(NormalizedVersionProvenance.id)
        .limit(2)
    )).all())
    if len(rows) != 1:
        return False
    source_type, generation, accepted_generation, provenance = rows[0]
    if provenance is not None and accepted_generation != generation:
        return False
    if source_type not in {"rss", "web", "api"}:
        return True
    if provenance is None:
        return False
    accepted_scope = provenance.get("provider_scope_discriminator")
    if not isinstance(accepted_scope, str) or len(accepted_scope) != 64:
        return False
    from modules.connectors import public as connectors

    current_scope = await connectors.get_current_provider_scope(
        session, source_id, expected_source_generation,
    )
    return current_scope is not None and current_scope.discriminator == accepted_scope


async def news_projection_scope_unavailable(
    session: AsyncSession, document_id: UUID, expected_source_generation: int,
) -> bool:
    """Report only whether a supported provider scope fence prevents News evidence output.

    The result contains no source configuration, item identifiers, or counts. Deleted,
    replaced, inactive, or stale-generation documents are not classified as scope failures.
    """
    row = (await session.execute(
        select(Document, DocumentVersion, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.generation == expected_source_generation,
        )
    )).one_or_none()
    if row is None:
        return False
    _document, version, source = row
    if source.type not in {"rss", "web", "api"}:
        return False
    provenance_rows = list((await session.scalars(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_version_id == version.id,
        ).order_by(
            NormalizedVersionProvenance.accepted_record_hash,
            NormalizedVersionProvenance.normalization_version,
        ).limit(2)
    )).all())
    if len(provenance_rows) != 1 or provenance_rows[0].source_generation != source.generation:
        return True
    accepted_scope = provenance_rows[0].provenance_json.get("provider_scope_discriminator")
    if not isinstance(accepted_scope, str) or len(accepted_scope) != 64:
        return True
    from modules.connectors import public as connectors

    current_scope = await connectors.get_current_provider_scope(session, source.id, source.generation)
    return current_scope is None or accepted_scope != current_scope.discriminator


async def news_current_scope_status(
    session: AsyncSession, source_ids: tuple[UUID, ...],
) -> NewsProjectionStatus:
    """Summarize current provider-scope omissions for at most 100 selected documents.

    Only fixed reason codes leave Documents; provider settings, credentials, item IDs and
    omission counts stay inside the owner module. A capped scan reports incompleteness.
    """
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids):
        raise ValueError("News scope status requires 1 to 32 unique sources")
    rows: list[Any] = list((await session.execute(
        select(
            Document.id, Source.id, Source.type, Source.generation,
            NormalizedVersionProvenance.source_generation,
            NormalizedVersionProvenance.provenance_json,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(
            NormalizedVersionProvenance,
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
        )
        .where(
            Document.source_id.in_(source_ids),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
        )
        .order_by(Document.id, NormalizedVersionProvenance.id)
        .limit(101)
    )).all())
    reasons: set[str] = set()
    if len(rows) > 100:
        reasons.add("candidate_scan_limit")
    source_scope: dict[UUID, str | None] = {}
    provider_types = {"rss", "web", "api"}
    for _document_id, source_id, source_type, generation, _accepted_generation, _provenance in rows[:100]:
        if source_type in provider_types and source_id not in source_scope:
            from modules.connectors import public as connectors

            snapshot = await connectors.get_current_provider_scope(session, source_id, generation)
            source_scope[source_id] = snapshot.discriminator if snapshot else None
    for _document_id, source_id, source_type, generation, accepted_generation, provenance in rows[:100]:
        if source_type not in provider_types:
            continue
        accepted_scope = provenance.get("provider_scope_discriminator") if isinstance(provenance, dict) else None
        if (
            source_scope.get(source_id) is None or accepted_generation != generation
            or not isinstance(accepted_scope, str) or accepted_scope != source_scope[source_id]
        ):
            reasons.add("scope_unavailable")
    return NewsProjectionStatus(incomplete_reasons=tuple(sorted(reasons)))


async def list_news_document_projections(
    session: AsyncSession, *, source_ids: tuple[UUID, ...], limit: int = 50,
    cursor: str | None = None, observed_since: datetime | None = None,
    channel_ids: tuple[str, ...] | None = None, language: str | None = None,
) -> tuple[list[NewsDocumentProjection], str | None]:
    """Page bounded current ready versions from explicitly authorized active sources."""
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids) or not 1 <= limit <= 100:
        raise ValueError("News source page must contain 1 to 32 unique sources and a bounded limit")
    if channel_ids is not None and (
        len(channel_ids) > 32 or len(set(channel_ids)) != len(channel_ids)
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("Channel scope must contain at most 32 unique numeric identifiers")
    statement = (
        select(Document.id, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids), Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")), Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )
    if observed_since is not None:
        statement = statement.where(func.coalesce(Document.observed_at, DocumentVersion.observed_at) >= observed_since)
    if language is not None:
        if language not in FEED_LANGUAGES:
            raise ValueError("Language filter must be an allowlisted code")
        # Unknown (NULL) language never matches a concrete language; only "Any" (None) returns it.
        statement = statement.where(or_(
            func.lower(Document.language) == language, func.lower(Document.language).like(f"{language}-%"),
        ))
    if channel_ids is not None:
        statement = statement.where(
            Source.provider == "telegram",
            select(NormalizedVersionProvenance.id).where(
                NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
                NormalizedVersionProvenance.source_generation == Source.generation,
                NormalizedVersionProvenance.provenance_json["provider_record"]["telegram"]["channel_id"].astext.in_(channel_ids),
            ).exists(),
        )
    if cursor:
        created_at, document_cursor = _decode_news_projection_cursor(cursor)
        statement = statement.where(tuple_(Document.created_at, Document.id) < (created_at, document_cursor))
    rows = list((await session.execute(statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    projections = []
    for document_id, _created_at in rows:
        item = await get_news_document_projection(session, document_id)
        if item is not None and item.source_id in source_ids:
            if channel_ids is not None and (
                item.provider_metadata is None
                or item.provider_metadata.provider != "telegram"
                or item.provider_metadata.telegram is None
                or item.provider_metadata.telegram.channel_id not in channel_ids
            ):
                continue
            projections.append(item)
    next_cursor = _encode_news_projection_cursor(rows[-1][1], rows[-1][0]) if more and rows else None
    return projections, next_cursor


FEED_LANGUAGES = frozenset({"en", "vi", "fr", "de", "es", "pt", "it", "ru", "ja", "ko", "zh", "id", "th"})
FEED_MAX_WINDOW = timedelta(days=366)


async def list_gadget_document_projections(
    session: AsyncSession, *, owner_id: int, source_ids: tuple[UUID, ...], limit: int = 50,
    cursor: str | None = None, channel_ids: tuple[str, ...] | None = None,
    language: str | None = None, since: datetime | None = None,
) -> GadgetDocumentProjectionList:
    """Return active, current, ready source records as a small dashboard projection page.

    Documents retains source-generation and provider-scope validation. Only short excerpts and
    typed immutable provider fields leave this owner boundary; full text stays out of dashboard APIs.
    """
    if since is not None:
        now = datetime.now(UTC)
        if since.tzinfo is None or not now - FEED_MAX_WINDOW <= since <= now + timedelta(minutes=5):
            raise ValueError("Time filter must be a timezone-aware instant within the last year")
    projections, next_cursor = await list_news_document_projections(
        session, source_ids=source_ids, limit=limit, cursor=cursor,
        channel_ids=channel_ids, language=language, observed_since=since,
    )
    version_ids = [item.document_version_id for item in projections]
    interaction_rows = (await session.scalars(
        select(DocumentInteraction).where(
            DocumentInteraction.owner_id == owner_id,
            DocumentInteraction.document_version_id.in_(version_ids),
        )
    )).all() if version_ids else []
    interactions = {row.document_version_id: row for row in interaction_rows}
    items = [
        _as_gadget_document_projection(
            item, interactions.get(item.document_version_id),
        )
        for item in projections
    ]
    return GadgetDocumentProjectionList(items=items, next_cursor=next_cursor)


def _as_gadget_document_projection(
    item: NewsDocumentProjection, interaction: DocumentInteraction | None = None,
) -> GadgetDocumentProjectionRead:
    """Project typed current source fields while omitting provider secrets and raw media handles."""
    provider_metadata = item.provider_metadata
    safe_provider_metadata = None
    if provider_metadata is not None:
        safe_telegram = None
        if provider_metadata.telegram is not None:
            detail = provider_metadata.telegram
            safe_telegram = GadgetTelegramRecordRead(
                channel_id=detail.channel_id,
                message_id=detail.message_id,
                thread_id=detail.thread_id,
                reply_to_message_id=detail.reply_to_message_id,
                channel_label=detail.channel_label,
                channel_username=detail.channel_username,
                edited_received=detail.edited_received,
                published_at=detail.published_at,
                edited_at=detail.edited_at,
                media=[GadgetTelegramMediaRead(
                    kind=media.kind, caption=media.caption, count=media.count,
                ) for media in detail.media],
            )
        safe_provider_metadata = GadgetProviderMetadataRead(
            provider=provider_metadata.provider,
            source_fields=provider_metadata.source_fields,
            telegram=safe_telegram,
        )
    return GadgetDocumentProjectionRead(
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        version_number=item.version_number,
        source_id=item.source_id,
        title=item.title,
        canonical_url=item.canonical_url,
        published_at=item.published_at,
        observed_at=item.observed_at,
        excerpt="\n\n".join(chunk.content for chunk in item.chunks)[:2000],
        provider_metadata=safe_provider_metadata,
        metadata_is_version_snapshot=item.metadata_is_version_snapshot,
        read_at=interaction.read_at if interaction else None,
        bookmarked_at=interaction.bookmarked_at if interaction else None,
    )


async def list_gadget_highlight_projection_page(
    session: AsyncSession, *, source_ids: tuple[UUID, ...], limit: int = 100,
    cursor_created_at: datetime | None = None, cursor_version_id: UUID | None = None,
) -> GadgetHighlightProjectionPage:
    """Page current accepted evidence by immutable-version creation order for durable highlight scans.

    New revisions of old documents receive new version IDs/timestamps and enter the forward scan.
    The cursor advances over candidate versions even when current-scope validation rejects one.
    """
    if (
        not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids)
        or not 1 <= limit <= 100
        or (cursor_created_at is None) != (cursor_version_id is None)
    ):
        raise ValueError("Highlight projection page has an invalid source set, limit, or version cursor")
    statement = (
        select(Document.id, DocumentVersion.id, DocumentVersion.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids),
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )
    if cursor_created_at is not None and cursor_version_id is not None:
        statement = statement.where(
            tuple_(DocumentVersion.created_at, DocumentVersion.id) > (cursor_created_at, cursor_version_id)
        )
    rows = list((await session.execute(
        statement.order_by(DocumentVersion.created_at, DocumentVersion.id).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    candidates = rows[:limit]
    items: list[GadgetDocumentProjectionRead] = []
    fences: list[GadgetDocumentSelectionFence] = []
    for document_id, version_id, _created_at in candidates:
        projection = await get_news_document_projection(session, document_id)
        if (
            projection is None or projection.document_version_id != version_id
            or projection.source_id not in source_ids
        ):
            continue
        items.append(_as_gadget_document_projection(projection))
        fences.append(GadgetDocumentSelectionFence(
            document_id=projection.document_id,
            document_version_id=projection.document_version_id,
            source_id=projection.source_id,
            source_generation=projection.current_source_generation,
            source_type=projection.source_type,
            provider=projection.provider,
            local_only=projection.local_only,
            scope_discriminator=projection.scope_discriminator,
        ))
    last = candidates[-1] if candidates else None
    return GadgetHighlightProjectionPage(
        items=items,
        selection_fences=fences,
        cursor_created_at=last[2] if last else None,
        cursor_version_id=last[1] if last else None,
        has_more=has_more,
    )


async def set_gadget_document_interaction(
    session: AsyncSession, *, owner_id: int, document_id: UUID, version_number: int,
    payload: GadgetDocumentInteractionPatch,
) -> GadgetDocumentInteractionRead | None:
    """Persist exact-version read/bookmark state only while that version is active and current."""
    # Serialize a first insert and competing read/bookmark changes against this document row.
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        return None
    projection = await get_news_document_projection(session, document_id)
    if projection is None or projection.version_number != version_number:
        return None
    row = await session.get(
        DocumentInteraction, (owner_id, projection.document_version_id),
    )
    now = datetime.now(UTC)
    read_at = (now if payload.read else None) if payload.read is not None else (row.read_at if row else None)
    bookmarked_at = (now if payload.bookmarked else None) if payload.bookmarked is not None else (row.bookmarked_at if row else None)
    if read_at is None and bookmarked_at is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        row = DocumentInteraction(
            owner_id=owner_id, document_version_id=projection.document_version_id,
            read_at=read_at, bookmarked_at=bookmarked_at,
        )
        session.add(row)
    else:
        row.read_at = read_at
        row.bookmarked_at = bookmarked_at
        row.updated_at = now
    await session.commit()
    return GadgetDocumentInteractionRead(
        document_version_id=projection.document_version_id,
        read_at=read_at, bookmarked_at=bookmarked_at,
    )


def _encode_news_projection_cursor(created_at: datetime, document_id: UUID) -> str:
    """Encode the bounded News projection keyset as canonical unpadded URL-safe base64."""
    raw = f"{created_at.isoformat()}|{document_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_news_projection_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode and validate a canonical projection cursor without exposing parse errors."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        timestamp, identifier = raw.split("|", 1)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or _encode_news_projection_cursor(parsed, UUID(identifier)) != cursor:
            raise ValueError
        return parsed, UUID(identifier)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid News projection cursor") from exc




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


def content_hash(content: str) -> str:
    """Return the SHA-256 digest of UTF-8 encoded document content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def ensure_demo_article(session: AsyncSession) -> tuple[int, int, int]:
    """Normalize one fictional article through Documents and preserve provenance and chunks.

    Returns (created, existing, skipped). The stable source/provider identity and accepted content
    hash make retries idempotent; a tombstoned normalized identity stays deleted. The caller owns
    the transaction and receipt, while Documents owns source fencing, normalization, and chunking.
    """
    from core.demo_seed import P12_DEMO_NAMESPACE, p12_demo_seed_id

    article_id = p12_demo_seed_id("article", "lantern-inscription-care")
    source_id = p12_demo_seed_id("article", "source")
    content = (
        "Fictional field note: Mira records that the north orchard lantern inscriptions should be "
        "photographed in soft morning light before the catalogue is assembled."
    )
    await sources.ensure_demo_source(session, source_id, P12_DEMO_NAMESPACE)
    source = await sources.lock_source(session, source_id)
    if source is None:
        raise RuntimeError("P12 demo article source is unavailable")
    title = "Field note: caring for orchard lantern inscriptions"
    result = await upsert_normalized_document(session, NormalizedDocumentInput(
        source_id=source_id,
        expected_source_generation=source.generation,
        observation_id=article_id,
        provider_id=f"{P12_DEMO_NAMESPACE}/lantern-inscription-care",
        accepted_record_hash=content_hash(content),
        normalization_version=1,
        observed_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        title=title,
        canonical_url="https://example.invalid/demo/orchard-lantern-care",
        content_type="article",
        content=content,
        provenance={"title": title, "content_type": "article"},
    ))
    if result.disposition == "tombstoned":
        return 0, 0, 1
    return (1, 0, 0) if result.created_version else (0, 1, 0)


async def list_evidence_ref_keys(
    session: AsyncSession, *, document_id: UUID | None = None, source_id: UUID | None = None,
    limit: int = 10_000,
) -> list[tuple[UUID, UUID]]:
    """List version/chunk evidence keys for exactly one bounded document or source."""
    if (document_id is None) == (source_id is None) or not 1 <= limit <= 10_000:
        raise ValueError("Specify one document or source and a bounded limit")
    statement = (
        select(DocumentVersion.id, DocumentChunk.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
    )
    statement = statement.where(Document.id == document_id) if document_id else statement.where(Document.source_id == source_id)
    rows = list((await session.execute(statement.order_by(DocumentVersion.id, DocumentChunk.id).limit(limit + 1))).all())
    if len(rows) > limit:
        raise ValueError("Evidence cleanup exceeds its atomic support limit")
    return [(version_id, chunk_id) for version_id, chunk_id in rows]


async def add_content_chunks(session: AsyncSession, version: DocumentVersion) -> int:
    """Chunk a version's content, add its searchable rows, and return their count."""
    await session.flush()
    drafts = await to_thread_joined(chunk_text, version.content)
    for index, draft in enumerate(drafts):
        session.add(DocumentChunk(
            document_version_id=version.id, chunk_index=index, content=draft.content,
            content_hash=content_hash(draft.content), token_count=draft.token_count,
            metadata_json=draft.metadata,
        ))
    return len(drafts)


async def backfill_current_chunks(session: AsyncSession, limit: int = 2) -> int:
    """Fill legacy manual revisions created before chunking was enabled."""
    versions = list((await session.scalars(
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            DocumentVersion.content != "",
            ~select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
        .order_by(DocumentVersion.id).limit(limit)
    )).all())
    for version in versions:
        hint = await session.get(Document, version.document_id)
        source = await sources.lock_source(session, hint.source_id) if hint else None
        document = await session.scalar(
            select(Document).where(Document.id == version.document_id).with_for_update()
        )
        if (
            source is None or source.status != "active" or document is None
            or document.current_version != version.version_number
        ):
            continue
        if await add_content_chunks(session, version):
            await _publish_document_ready(session, document, version)
    if versions:
        await session.commit()
    return len(versions)


async def raw_uris(session: AsyncSession, source_id: UUID | None = None) -> set[str]:
    """Return nonempty raw-storage URIs globally or for one source."""
    statement = select(Document.raw_uri).where(Document.raw_uri.is_not(None))
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    return {uri for uri in (await session.scalars(statement)).all() if uri}


async def raw_uri_is_referenced(session: AsyncSession, raw_uri: str) -> bool:
    """Return whether a surviving document still owns the exact raw-storage URI."""
    return bool(await session.scalar(select(Document.id).where(Document.raw_uri == raw_uri).limit(1)))


async def lock_raw_uri_identity(session: AsyncSession, raw_uri: str) -> None:
    """Serialize raw-URI publication with its final ownership check and unlink transaction."""
    identity = f"documents.raw:{raw_uri}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


async def get_document_cleanup_operation(
    session: AsyncSession, operation_id: UUID,
) -> DocumentCleanupOperation | None:
    """Read a Documents-owned cleanup receipt without exposing its captured raw URI."""
    return await session.get(DocumentCleanupOperation, operation_id)


async def capture_document_cleanup_evidence(session: AsyncSession, operation: DocumentCleanupOperation) -> None:
    """Snapshot exact version-only and chunk identities into the operation before its FK cascade.

    Both inserts are owner-local SQL ``INSERT … SELECT`` statements, so document history size
    does not create an unbounded Python snapshot. The child identities intentionally have no
    foreign keys back to evidence rows and remain readable until the cleanup receipt is removed.
    """
    # DB-recorded bound for legacy brief coverage; read before the canonical rows are deleted.
    operation.earliest_version_created_at = await session.scalar(
        select(func.min(DocumentVersion.created_at)).where(DocumentVersion.document_id == operation.document_id)
    )
    identity_columns = ["id", "operation_id", "document_version_id", "chunk_id", "reference_kind"]
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        identity_columns,
        select(
            func.gen_random_uuid(), literal(operation.id), DocumentVersion.id,
            literal(None), literal("version"),
        ).where(DocumentVersion.document_id == operation.document_id),
    ))
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        identity_columns,
        select(
            func.gen_random_uuid(), literal(operation.id), DocumentChunk.document_version_id,
            DocumentChunk.id, literal("chunk"),
        ).join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .where(DocumentVersion.document_id == operation.document_id),
    ))


async def list_document_cleanup_evidence_scope(
    session: AsyncSession,
    operation_id: UUID,
    *,
    after: UUID | None = None,
    limit: int = 100,
) -> DocumentCleanupEvidenceScope | None:
    """Return one deterministic bounded identity page for the durable Chat cleanup cursor."""
    if not 1 <= limit <= 100:
        raise ValueError("Document cleanup evidence page size must be between 1 and 100")
    operation = await session.get(DocumentCleanupOperation, operation_id)
    if operation is None:
        return None
    statement = select(DocumentCleanupEvidenceReference).where(
        DocumentCleanupEvidenceReference.operation_id == operation_id,
    )
    if after is not None:
        statement = statement.where(DocumentCleanupEvidenceReference.id > after)
    rows = list((await session.scalars(
        statement.order_by(DocumentCleanupEvidenceReference.id).limit(limit + 1)
    )).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return DocumentCleanupEvidenceScope(
        operation_id=operation.id,
        source_id=operation.source_id,
        document_id=operation.document_id,
        references=tuple(
            DocumentCleanupEvidenceIdentity(
                document_version_id=row.document_version_id,
                chunk_id=row.chunk_id,
                reference_kind=row.reference_kind,
            )
            for row in rows
        ),
        next_cursor=rows[-1].id if has_more and rows else None,
    )


async def source_cleanup_progress(
    session: AsyncSession,
    source_purge_operation_id: UUID,
    *,
    source_id: UUID,
    capture_recorded: bool,
) -> SourceCleanupProgress:
    """Aggregate every retained same-source receipt in one SQL statement; return counts only.

    The Source owner verified that ``source_id`` owns the operation and supplies its durable
    capture receipt, because only that owner can tell a truly empty Source from a legacy
    operation whose rows vanished. The query is an exact ``source_id`` index aggregate (one output
    row, fixed columns); each receipt is counted once whether it is linked to this operation,
    linked to an older one, or unlinked, and no receipt is mutated, reparented or locked. Row
    work grows with retained receipt history for the Source (capacity unverified). Every stage is
    read afresh: a stage that is pending or failed is never cached as complete. A receipt whose
    captured evidence identities are unavailable is failed; one still ``capturing`` is pending.
    Memory cache eviction is an independent pending obligation until its postcommit retry clears it.
    """
    failed = or_(
        DocumentCleanupOperation.evidence_scope_status == "unavailable",
        DocumentCleanupOperation.raw_status == "failed",
        DocumentCleanupOperation.chat_status == "failed",
        DocumentCleanupOperation.memory_status == "failed",
        DocumentCleanupOperation.agent_status == "failed",
        DocumentCleanupOperation.materialization_status == "failed",
        DocumentCleanupOperation.brief_status == "failed",
        DocumentCleanupOperation.copied_status == "failed",
    )
    scope_pending = DocumentCleanupOperation.evidence_scope_status == "capturing"
    raw_pending = DocumentCleanupOperation.raw_status.not_in(
        ("not_present", "retained_shared", "succeeded", "failed")
    )
    chat_pending = DocumentCleanupOperation.chat_status.not_in(("succeeded", "failed"))
    memory_pending = or_(
        DocumentCleanupOperation.memory_status != "succeeded",
        DocumentCleanupOperation.memory_cache_pending.is_(True),
    )
    agent_pending = DocumentCleanupOperation.agent_status.not_in(("succeeded", "failed"))
    materialization_pending = DocumentCleanupOperation.materialization_status.not_in(("succeeded", "failed"))
    brief_pending = DocumentCleanupOperation.brief_status.not_in(("succeeded", "failed"))
    copy_pending = DocumentCleanupOperation.copied_status != "succeeded"
    # Cache eviction is a separate durable Memory obligation, including while a prior
    # content-cleanup stage is terminally failed and awaits its retryable postcommit work.
    pending = or_(
        DocumentCleanupOperation.memory_cache_pending.is_(True),
        and_(~failed, or_(
            scope_pending, raw_pending, chat_pending, memory_pending, agent_pending,
            materialization_pending, brief_pending, copy_pending,
        )),
    )
    linked = DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id
    # NULL linkage must count as historical: ``NOT (NULL = x)`` is NULL, so test it explicitly.
    unlinked = or_(
        DocumentCleanupOperation.source_purge_operation_id.is_(None),
        DocumentCleanupOperation.source_purge_operation_id != source_purge_operation_id,
    )
    receipt_id = DocumentCleanupOperation.id
    memory_waiting_expr = or_(
        and_(~failed, memory_pending), DocumentCleanupOperation.memory_cache_pending.is_(True),
    )
    row = (await session.execute(select(
        func.count(receipt_id).filter(linked),
        func.count(receipt_id).filter(unlinked),
        func.count(receipt_id).filter(linked, pending),
        func.count(receipt_id).filter(linked, failed),
        func.count(receipt_id).filter(unlinked, pending),
        func.count(receipt_id).filter(unlinked, failed),
        func.count(receipt_id).filter(and_(~failed, raw_pending)),
        func.count(receipt_id).filter(and_(~failed, chat_pending)),
        func.count(receipt_id).filter(memory_waiting_expr),
        func.count(receipt_id).filter(and_(~failed, agent_pending)),
        func.count(receipt_id).filter(and_(~failed, materialization_pending)),
        func.count(receipt_id).filter(and_(~failed, brief_pending)),
        func.count(receipt_id).filter(linked, ~failed, or_(raw_pending, chat_pending)),
    ).where(DocumentCleanupOperation.source_id == source_id))).one()
    (child_count, historical_count, pending_count, failed_count, historical_pending, historical_failed,
     raw_waiting, chat_waiting, memory_waiting, agent_waiting, materialization_waiting, brief_waiting,
     linked_active) = (int(value or 0) for value in row)
    owners: list[str] = []
    if not capture_recorded:
        owners.append("documents")
    if raw_waiting:
        owners.append("raw")
    if chat_waiting:
        owners.append("chat")
    if memory_waiting:
        owners.append("memory")
    if agent_waiting:
        owners.append("agents")
    if materialization_waiting:
        owners.extend(("notifications", "automations"))
    if brief_waiting:
        owners.append("dashboard")
    # Complete only when every required stage of every retained receipt succeeded; a terminal
    # unavailable stage is failed (counted above), never relabeled complete.
    all_required_complete = (
        capture_recorded and pending_count + historical_pending == 0 and failed_count + historical_failed == 0
    )
    return SourceCleanupProgress(
        child_count=child_count,
        capture_complete=capture_recorded,
        pending_count=pending_count,
        failed_count=failed_count,
        all_required_complete=all_required_complete,
        pending_owner_codes=tuple(owners),
        historical_count=historical_count,
        historical_pending_count=historical_pending,
        historical_failed_count=historical_failed,
        active_copy_work=bool(linked_active),
    )


async def publish_source_cleanup_wakeup(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    progress_key: str,
) -> None:
    """Publish idempotent Source aggregate events for one durable child-stage transition.

    The child receipt transaction owns this outbox change. Payloads identify only Source purge
    operations, so this path never locks Source rows or Sources models under a URI lock. The
    linked operation (if any) keeps its original deterministic event ID. Every other unfinished
    same-source operation, including historical NULL linkage, is found through the Sources public
    observer seam (<=100 exact IDs; unfinished coverage first) and receives a stable
    observer-specific UUID. Observers beyond the first page are not dropped: Sources' persisted
    coverage reconciler re-arms every unfinished operation independently of these hints.
    The caller supplies a bounded ASCII status/revision token with no content data while holding
    the child receipt lock; deriving distinct UUIDs deduplicates repeated transitions without
    allowing an older observer to consume a newer wakeup.
    """
    if not re.fullmatch(r"[a-z0-9:_;=-]{1,128}", progress_key):
        raise ValueError("Source cleanup progress key must be a bounded lowercase status token")
    from modules.ingestion import public as ingestion

    linked = operation.source_purge_operation_id
    targets: list[tuple[UUID, UUID]] = []
    if linked is not None:
        targets.append((linked, uuid5(operation.id, f"source-purge-progress:{progress_key}")))
    for observer_id in await sources.list_source_purge_observer_ids(session, operation.source_id, limit=100):
        if observer_id != linked:
            targets.append((observer_id, uuid5(
                operation.id, f"source-purge-progress:{observer_id}:{progress_key}",
            )))
    for target_id, event_id in targets:
        if await ingestion.get_event_delivery(session, event_id) is None:
            await ingestion.publish_event(session, DomainEvent(
                id=event_id,
                type="source.purge.progressed",
                version=1,
                occurred_at=datetime.now(UTC),
                producer="modules.knowledge.documents",
                payload={"operation_id": str(target_id)},
            ))


async def create_document(session: AsyncSession, payload: DocumentCreate) -> Document:
    """Create a source-locked document and initial version, then publish its change."""
    await sources.lock_source_for_document(session, payload.source_id)
    digest = content_hash(payload.content)
    document = Document(
        source_id=payload.source_id,
        external_id=payload.external_id,
        title=payload.title,
        metadata_json=payload.metadata,
        current_version=1,
        content_hash=digest,
    )
    session.add(document)
    try:
        await session.flush()
        version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content=payload.content,
                content_hash=digest,
        )
        session.add(version)
        if await add_content_chunks(session, version):
            await _publish_document_ready(session, document, version)
        await commit_with_replay(
            session,
            [make_knowledge_change(payload.source_id, document.id, 1)],
        )
    except IntegrityError:
        await session.rollback()
        raise
    await session.refresh(document)
    return document


async def document_metadata(session: AsyncSession, document_ids: list[UUID]) -> dict[UUID, tuple[str, str | None]]:
    """Return ``{document_id: (title, mime_type)}`` for the given ids (metadata only, never content).

    Read-only batch projection for the automations producer sweep; callers pass at most one
    sweep page of ids. Unknown ids are simply absent.
    """
    rows = await session.execute(select(Document.id, Document.title, Document.mime_type).where(
        Document.id.in_(document_ids)))
    return {row[0]: (row[1], row[2]) for row in rows.all()}


async def get_document(session: AsyncSession, document_id: UUID) -> Document | None:
    """Fetch a document by primary key without applying additional visibility filters."""
    return await session.get(Document, document_id)


async def get_tool_document(
    session: AsyncSession, document_id: UUID, *, source_ids: frozenset[UUID],
    owner_all: bool = False, destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolDocumentRead | None:
    """Read a query-time active/current document DTO under exact source and destination fences.

    Local-only source rows are excluded in SQL for remote destinations. An empty non-owner
    source set returns no rows. This query-time projection does not replace revalidation by
    the eventual sender immediately before remote transmission.
    """
    statement = (
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, Document.title,
               Document.content_type, DocumentVersion.version_number, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
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
    owner_all: bool = False,
    destination: ToolDestination = ToolDestination.REMOTE,
) -> bool:
    """Require every bounded native Knowledge result to remain an exact current document DTO.

    A single missing, changed, out-of-scope, inactive, unready or remote-local-only row denies the
    full page, including its cursor. The projection returns no persistence models or write access.
    """
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
    source_ids: frozenset[UUID], owner_all: bool = False,
    destination: ToolDestination = ToolDestination.LOCAL,
) -> ToolDocumentPage:
    """Page only active/current rows allowed by source and destination before cursor creation.

    Remote local-only rows are filtered in SQL before limit/keyset selection, so no hidden
    source/document identifier participates in the returned page or continuation cursor.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Document tool page size is outside its supported bound")
    statement = (
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, Document.title,
               Document.content_type, DocumentVersion.version_number, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
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


async def has_document_identity(session: AsyncSession, source_id: UUID, external_id: str) -> bool:
    """Check whether a source already owns the given external document ID."""
    return bool(
        await session.scalar(
            select(Document.id).where(Document.source_id == source_id, Document.external_id == external_id)
        )
    )


async def upsert_normalized_document(
    session: AsyncSession, payload: NormalizedDocumentInput
) -> NormalizedDocumentResult:
    """Persist one source-owned immutable normalized revision without committing.

    The source generation and provider provenance are checked before identity
    allocation; source, normalized identity, and document rows serialize writers.
    Generic providers select current content by observed time and accepted hash.
    Telegram requires owner-validated order, rejects a conflicting equal rank,
    and selects by observed time, epoch, then update ID. Structured world data
    leaves current-version selection to the observations owner, which orders the
    accepted ingestion identity and calls the exact-version selection contract
    below in this transaction. Version numbering remains independently monotonic;
    transaction commit and derived cleanup belong to the ingestion boundary.
    """
    source = await sources.lock_source(session, payload.source_id)
    if source is None or source.status != "active" or source.generation != payload.expected_source_generation:
        raise ValueError("Normalized source generation is no longer active")
    source_projection = await sources.get_connector_source(session, payload.source_id)
    if (
        source_projection is None
        or source_projection.id != source.id
        or source_projection.status != source.status
        or source_projection.generation != source.generation
    ):
        # Provider/type/configuration belong to this detached owner projection;
        # reject a stale or missing view while the source fence remains locked.
        raise ValueError("Normalized source projection no longer matches its lifecycle fence")
    provider_record = payload.provenance.get("provider_record")
    if provider_record is not None:
        typed_provider = ProviderRecordMetadata.model_validate(provider_record)
        if source_projection.provider != typed_provider.provider:
            raise ValueError("Provider provenance does not match the immutable source provider")
    elif source_projection.provider == "telegram":
        raise ValueError("Telegram normalization requires immutable delivery provenance")

    identity = await session.scalar(
        select(NormalizedDocumentIdentity)
        .where(
            NormalizedDocumentIdentity.source_id == payload.source_id,
            NormalizedDocumentIdentity.external_id == payload.provider_id,
        )
        .with_for_update()
    )
    created_identity = identity is None
    if identity is None:
        identity = NormalizedDocumentIdentity(source_id=payload.source_id, external_id=payload.provider_id)
        session.add(identity)
        await session.flush()
    if identity.tombstoned_at is not None:
        return NormalizedDocumentResult(
            disposition="tombstoned", document_id=None, document_version_id=None,
            version_number=None, created_version=False, selected_current=False, chunk_count=0,
        )

    document = await session.scalar(
        select(Document)
        .where(Document.source_id == payload.source_id, Document.external_id == payload.provider_id)
        .with_for_update()
    )
    if document is not None and identity.document_id not in (None, document.id):
        raise ValueError("Normalized identity points to a different document")
    if document is not None and identity.document_id is None:
        if created_identity:
            await session.delete(identity)
            await session.flush()
        raise ValueError("Provider identity conflicts with an existing non-normalized document")
    if document is None:
        document = Document(
            source_id=payload.source_id, external_id=payload.provider_id,
            title=payload.title, content_type=payload.content_type,
            canonical_url=payload.canonical_url, published_at=payload.published_at,
            observed_at=payload.observed_at, current_version=0,
            content_hash=content_hash(payload.content), extraction_status="ready",
        )
        session.add(document)
        await session.flush()
        identity.document_id = document.id

    prior = await session.scalar(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.accepted_record_hash == payload.accepted_record_hash,
            NormalizedVersionProvenance.normalization_version == payload.normalization_version,
        )
    )
    if prior is not None:
        version = await session.get(DocumentVersion, prior.document_version_id)
        if version is None:
            raise RuntimeError("Normalized provenance references a missing revision")
        count = await session.scalar(
            select(func.count()).select_from(DocumentChunk)
            .where(DocumentChunk.document_version_id == version.id)
        )
        return NormalizedDocumentResult(
            disposition="duplicate", document_id=document.id, document_version_id=version.id,
            version_number=version.version_number, created_version=False,
            selected_current=document.current_version == version.version_number,
            chunk_count=int(count or 0),
        )

    current_provenance = None
    if document.current_version:
        current_provenance = await session.scalar(
            select(NormalizedVersionProvenance)
            .join(DocumentVersion, DocumentVersion.id == NormalizedVersionProvenance.document_version_id)
            .where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version_number == document.current_version,
            )
        )
        if current_provenance is None:
            raise ValueError("Provider identity conflicts with an owner-authored current revision")
    if source_projection.provider in {"alpha_vantage", "open_meteo"}:
        # Observation acceptance time and ingestion identity, not provider event
        # time or worker arrival order, own structured-series current selection.
        selected = False
    elif source_projection.provider == "telegram":
        incoming_metadata = ProviderRecordMetadata.model_validate(provider_record)
        incoming_telegram = incoming_metadata.telegram
        if incoming_telegram is None or payload.telegram_order is None:
            raise ValueError("Telegram version ordering proof is missing")
        incoming_rank = (payload.observed_at, payload.telegram_order.epoch, payload.telegram_order.update_id)
        current_rank = None
        if current_provenance is not None:
            current_metadata = ProviderRecordMetadata.model_validate(
                current_provenance.provenance_json.get("provider_record")
            )
            current_telegram = current_metadata.telegram
            if current_telegram is None or current_telegram.bot_id != incoming_telegram.bot_id:
                raise ValueError("Telegram current version has an incompatible bot binding")
            current_rank = (
                current_provenance.selection_observed_at,
                current_telegram.epoch,
                current_telegram.update_id,
            )
        if current_rank == incoming_rank and current_provenance is not None:
            if current_provenance.accepted_record_hash != payload.accepted_record_hash:
                raise ValueError("Telegram delivery order has conflicting immutable content")
            raise ValueError("Telegram delivery proof already exists with a different normalization version")
        selected = current_rank is None or incoming_rank > current_rank
    else:
        current_hash_rank = (
            (current_provenance.selection_observed_at, current_provenance.accepted_record_hash)
            if current_provenance is not None else None
        )
        selected = current_hash_rank is None or (payload.observed_at, payload.accepted_record_hash) > current_hash_rank
    max_number = await session.scalar(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document.id)
    )
    version = DocumentVersion(
        document_id=document.id, version_number=int(max_number or 0) + 1,
        content=payload.content, content_hash=content_hash(payload.content),
        observed_at=payload.observed_at,
    )
    session.add(version)
    await session.flush()
    session.add(NormalizedVersionProvenance(
        document_id=document.id, document_version_id=version.id,
        provider_id=payload.provider_id, provider_version=payload.provider_version,
        accepted_record_hash=payload.accepted_record_hash,
        normalization_version=payload.normalization_version,
        source_generation=payload.expected_source_generation,
        observed_at=payload.observed_at, received_at=payload.received_at,
        collected_at=payload.collected_at, selection_observed_at=payload.observed_at,
        title=payload.title, canonical_url=payload.canonical_url,
        published_at=payload.published_at, content_type=payload.content_type,
        provenance_json=payload.provenance,
    ))
    chunk_count = await add_content_chunks(session, version)
    if selected:
        document.current_version = version.version_number
        document.content_hash = version.content_hash
        document.title = payload.title
        document.canonical_url = payload.canonical_url
        document.published_at = payload.published_at
        document.content_type = payload.content_type
        document.observed_at = payload.observed_at
        document.extraction_status = "ready"
    await session.flush()
    return NormalizedDocumentResult(
        disposition="normalized", document_id=document.id,
        document_version_id=version.id, version_number=version.version_number,
        created_version=True, selected_current=selected, chunk_count=chunk_count,
    )


async def select_current_world_document_version(
    session: AsyncSession, *, document_id: UUID, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
) -> bool:
    """Select one immutable world-data revision after Observations accepts it as current.

    The caller must make this call in the same ingestion transaction as the
    observation write. Documents owns current-version and metadata projection;
    accepted_at plus ingestion identity remain owned by Observations. The source
    generation, provider scope, document identity, and retained version provenance
    are rechecked before changing the pointer, so retries and delayed workers can
    only restore a version that the current acceptance decision already selected.
    """
    source_id = await session.scalar(select(Document.source_id).where(Document.id == document_id))
    if source_id is None:
        return False
    source = await sources.lock_source(session, source_id)
    if source is None or source.status != "active" or source.generation != expected_source_generation:
        return False
    source_projection = await sources.get_connector_source(session, source_id)
    if (
        source_projection is None or source_projection.status != "active"
        or source_projection.generation != expected_source_generation
        or source_projection.provider not in {"alpha_vantage", "open_meteo"}
    ):
        return False
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id).with_for_update()
    )
    if document is None:
        return False
    selected = (await session.execute(
        select(DocumentVersion, NormalizedVersionProvenance)
        .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(
            DocumentVersion.id == document_version_id,
            DocumentVersion.document_id == document.id,
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.provider_id == document.external_id,
            NormalizedVersionProvenance.source_generation == expected_source_generation,
        )
    )).one_or_none()
    if selected is None:
        return False
    version, provenance = selected
    if provenance.provenance_json.get("provider_scope_discriminator") != provider_scope_discriminator:
        return False
    document.current_version = version.version_number
    document.content_hash = version.content_hash
    document.title = provenance.title
    document.canonical_url = provenance.canonical_url
    document.published_at = provenance.published_at
    document.content_type = provenance.content_type
    document.observed_at = provenance.observed_at
    document.extraction_status = "ready"
    await session.flush()
    return True


async def add_uploaded_document(
    session: AsyncSession,
    source_id: UUID,
    title: str,
    mime_type: str,
    raw_uri: str,
    metadata: dict[str, object],
    external_id: str,
    document_id: UUID,
) -> UUID:
    """Publish one uploaded raw URI after the deletion fence and create its empty version.

    The caller has already staged the file and owns rollback cleanup. The raw-URI
    transaction lock serializes this publication with deletion; a URI already captured
    by a durable cleanup receipt raises ValueError so it cannot be referenced again.
    """
    await lock_raw_uri_identity(session, raw_uri)
    if await session.scalar(select(DocumentCleanupOperation.id).where(DocumentCleanupOperation.raw_uri == raw_uri)):
        raise ValueError("This raw file identity was already deleted")
    document = Document(
        id=document_id,
        source_id=source_id,
        external_id=external_id,
        title=title,
        content_type="file",
        mime_type=mime_type,
        raw_uri=raw_uri,
        metadata_json=metadata,
        current_version=1,
        content_hash=content_hash(""),
        extraction_status="queued",
    )
    session.add(document)
    await session.flush()
    session.add(DocumentVersion(document_id=document.id, version_number=1, content="", content_hash=content_hash("")))
    await session.flush()
    return document.id


async def lock_document_for_extraction(
    session: AsyncSession, document_id: UUID, source_id: UUID
) -> bool:
    """Lock and confirm a document belongs to the supplied source."""
    return await session.scalar(
        select(Document.id)
        .where(Document.id == document_id, Document.source_id == source_id)
        .with_for_update()
    ) is not None


async def set_extraction_status(
    session: AsyncSession, document_id: UUID, source_id: UUID, status: str
) -> bool:
    """Set extraction state only for a document owned by the supplied source."""
    result = await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.source_id == source_id)
        .values(extraction_status=status)
        .returning(Document.id)
    )
    return result.scalar_one_or_none() is not None


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
) -> UUID | None:
    """Save parser output under a locked active source, adding chunks only once."""
    source = await sources.lock_source(session, source_id)
    if source is None or source.status != "active":
        return None
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id).with_for_update()
    )
    if document is None:
        return None
    current = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == document.current_version,
        )
    )
    if current is None:
        raise RuntimeError("Current document version is missing")
    if current.content != text:
        digest = content_hash(text)
        max_number = await session.scalar(
            select(func.coalesce(func.max(DocumentVersion.version_number), 0))
            .where(DocumentVersion.document_id == document.id)
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
    document.extraction_status = extraction_status
    document.metadata_json = {
        **document.metadata_json,
        "extraction": dict(extraction_metadata),
        "warnings": list(warnings),
        "parser": parser,
    }
    existing = await session.scalar(
        select(DocumentChunk.id).where(DocumentChunk.document_version_id == current.id).limit(1)
    )
    if existing is None:
        for index, chunk in enumerate(chunks):
            content = str(chunk["content"])
            session.add(
                DocumentChunk(
                    document_version_id=current.id,
                    chunk_index=index,
                    content=content,
                    content_hash=content_hash(content),
                    token_count=int(cast("int", chunk["token_count"])),
                    metadata_json=dict(cast("dict[str, object]", chunk.get("metadata", {}))),
                )
            )
    if extraction_status == "succeeded" and chunks:
        await _publish_document_ready(session, document, current)
    await session.flush()
    return document.id


async def list_documents(
    session: AsyncSession, limit: int, cursor: str | None, source_id: UUID | None
) -> tuple[list[Document], str | None]:
    """Return a created-time-descending document page with an optional cursor."""
    statement = select(Document)
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    statement = statement.order_by(desc(Document.created_at), desc(Document.id))
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(Document.created_at, Document.id) < (timestamp, identifier)
        )
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return rows, next_cursor


async def update_document(
    session: AsyncSession, document: Document, payload: DocumentPatch
) -> Document:
    """Update supplied metadata fields, publish only actual changes, and refresh the row."""
    changed = False
    if "title" in payload.model_fields_set:
        value = payload.title or ""
        changed = changed or document.title != value
        document.title = value
    if "metadata" in payload.model_fields_set:
        metadata_value = payload.metadata or {}
        changed = changed or document.metadata_json != metadata_value
        document.metadata_json = metadata_value
    drafts = [make_knowledge_change(document.source_id, document.id, document.current_version)] if changed else []
    await commit_with_replay(session, drafts)
    await session.refresh(document)
    return document


async def delete_document(session: AsyncSession, document_id: UUID) -> DocumentCleanupOperation | None:
    """Commit access revocation, detached evidence IDs, and durable asynchronous cleanup stages.

    Lock order is Source then Document then raw-URI identity then normalized identity and
    graph support. The operation snapshots raw storage and exact immutable version/chunk IDs
    before canonical rows are removed; its event and deletion/tombstone replay commit atomically.
    A cleanup event is always emitted, including documents with no raw URI. The caller owns
    authorization; a successful return means canonical access is revoked, not that any cleanup
    owner stage has completed.
    """
    identity = await session.execute(select(Document.source_id).where(Document.id == document_id))
    source_id = identity.scalar_one_or_none()
    if source_id is None:
        return None
    source = await sources.lock_source(session, source_id)
    if source is None:
        return None
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id).with_for_update()
    )
    if document is None:
        return None
    if document.raw_uri:
        # Upload publication takes the same identity lock before accepting a new reference.
        await lock_raw_uri_identity(session, document.raw_uri)
    operation = DocumentCleanupOperation(
        source_id=source_id,
        document_id=document.id,
        raw_uri=document.raw_uri,
        raw_status="queued" if document.raw_uri else "not_present",
        evidence_scope_status="capturing",
        copied_status="queued",
        chat_status="queued",
        status="queued",
    )
    session.add(operation)
    await session.flush()
    await capture_document_cleanup_evidence(session, operation)
    operation.evidence_scope_status = "captured"
    from modules.ingestion import public as ingestion

    await ingestion.publish_event(session, DomainEvent(
        id=uuid5(operation.id, "document-cleanup-requested"),
        type="document.cleanup.requested",
        version=1,
        occurred_at=datetime.now(UTC),
        producer="modules.knowledge.documents",
        payload={"operation_id": str(operation.id)},
    ))
    from modules.knowledge.observations import public as observations
    await observations.purge_document_in_uow(session, document.id)
    if document.external_id is not None:
        doc_identity = await session.scalar(
            select(NormalizedDocumentIdentity).where(
                NormalizedDocumentIdentity.source_id == source_id,
                NormalizedDocumentIdentity.external_id == document.external_id,
            ).with_for_update()
        )
        if doc_identity is None:
            doc_identity = NormalizedDocumentIdentity(
                source_id=source_id,
                external_id=document.external_id,
                document_id=document.id,
            )
        session.add(doc_identity)
        await session.flush()
        doc_identity.tombstoned_at = datetime.now(UTC)
        doc_identity.document_id = None
        from modules.ingestion import public as ingestion
        await ingestion.tombstone_document_materializations(session, document.id)
    timeline_drafts = await _remove_graph_support(
        session, document_id=document_id, replay_source_id=source_id,
    )
    result = await session.scalars(
        delete(Document)
        .where(Document.id == document_id, Document.source_id == source_id)
        .returning(Document.id)
    )
    deleted = result.first() is not None
    drafts: list[ReplayDraft] = [*timeline_drafts]
    if deleted:
        drafts.append(make_knowledge_change(source_id, document_id, deleted=True))
    if not deleted:
        raise RuntimeError("Locked document disappeared during its cleanup transaction")
    await commit_with_replay(session, drafts)
    await session.refresh(operation)
    return operation


async def _capture_source_document_cleanup(
    session: AsyncSession, source_id: UUID, source_purge_operation_id: UUID,
) -> None:
    """Create one durable cleanup receipt and exact evidence children for every source document.

    The caller owns the Source lock, transaction, and already-locked Documents set. This helper
    locks raw identities in exact URI order,
    then uses owner-local INSERT SELECT statements so versions, chunks, and URIs are never
    assembled into an unbounded Python snapshot. Child events commit with the canonical cascade.
    """
    # Materialize and order identities in PostgreSQL; the lock calls do not emit URI values to Python.
    await session.execute(text(
        "WITH identities AS MATERIALIZED ("
        " SELECT DISTINCT raw_uri FROM documents"
        " WHERE source_id = :source_id AND raw_uri IS NOT NULL AND raw_uri <> ''"
        " ORDER BY raw_uri"
        "), locks AS MATERIALIZED ("
        " SELECT pg_advisory_xact_lock(hashtextextended('documents.raw:' || raw_uri, 0)) AS acquired"
        " FROM identities ORDER BY raw_uri"
        ") SELECT count(*) FROM locks"
    ), {"source_id": source_id})

    await session.execute(insert(DocumentCleanupOperation).from_select(
        [
            "id", "source_id", "document_id", "source_purge_operation_id", "raw_uri",
            "record_status", "graph_status", "raw_status", "evidence_scope_status",
            "copied_status", "chat_status", "status", "earliest_version_created_at",
        ],
        select(
            func.gen_random_uuid(), Document.source_id, Document.id,
            literal(source_purge_operation_id), Document.raw_uri,
            literal("deleted"), literal("tombstoned"),
            case(((Document.raw_uri.is_not(None) & (Document.raw_uri != "")), "queued"), else_="not_present"),
            literal("capturing"), literal("queued"), literal("queued"), literal("queued"),
            select(func.min(DocumentVersion.created_at))
            .where(DocumentVersion.document_id == Document.id).scalar_subquery(),
        ).where(Document.source_id == source_id),
    ))

    columns = ["id", "operation_id", "document_version_id", "chunk_id", "reference_kind"]
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        columns,
        select(
            func.gen_random_uuid(), DocumentCleanupOperation.id, DocumentVersion.id,
            literal(None), literal("version"),
        ).join(DocumentCleanupOperation, and_(
            DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
            DocumentCleanupOperation.document_id == DocumentVersion.document_id,
        )),
    ))
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        columns,
        select(
            func.gen_random_uuid(), DocumentCleanupOperation.id, DocumentChunk.document_version_id,
            DocumentChunk.id, literal("chunk"),
        ).join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(DocumentCleanupOperation, and_(
            DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
            DocumentCleanupOperation.document_id == DocumentVersion.document_id,
        )),
    ))
    await session.execute(update(DocumentCleanupOperation).where(
        DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
    ).values(evidence_scope_status="captured"))

    # At most 10,000 receipts exist by the gate above; keyset them to bound event batches.
    after: UUID | None = None
    while True:
        statement = select(DocumentCleanupOperation.id).where(
            DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
        )
        if after is not None:
            statement = statement.where(DocumentCleanupOperation.id > after)
        receipt_ids = list((await session.scalars(
            statement.order_by(DocumentCleanupOperation.id).limit(500)
        )).all())
        if not receipt_ids:
            break
        now = datetime.now(UTC)
        from modules.ingestion import public as ingestion

        for receipt_id in receipt_ids:
            await ingestion.publish_event(session, DomainEvent(
                id=uuid5(receipt_id, "document-cleanup-requested"),
                type="document.cleanup.requested",
                version=1,
                occurred_at=now,
                producer="modules.knowledge.documents",
                payload={"operation_id": str(receipt_id)},
            ))
        after = receipt_ids[-1]


async def delete_source_documents(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_purge_operation_id: UUID,
) -> list[ReplayDraft]:
    """Capture exact cleanup children before deleting source-owned canonical data.

    The caller holds the Source row and commits this unit of work. The Documents owner locks
    at most 10,001 rows to enforce its existing 10,000-document atomic ceiling, serializes raw
    URI tombstones with publication, and emits bounded child events in the same transaction;
    no filesystem work or cross-owner row mutation occurs here.
    """
    document_ids = list((await session.scalars(
        select(Document.id).where(Document.source_id == source_id).order_by(Document.id).limit(10_001).with_for_update()
    )).all())
    if len(document_ids) > 10_000:
        raise ValueError("Source graph cleanup exceeds its atomic document limit")
    await _capture_source_document_cleanup(session, source_id, source_purge_operation_id)
    from modules.knowledge.observations import public as observations
    await observations.purge_source_in_uow(session, source_id)
    timeline_drafts = await _remove_graph_support(session, source_id=source_id)
    await session.execute(
        delete(NormalizedDocumentIdentity).where(NormalizedDocumentIdentity.source_id == source_id)
    )
    await session.execute(delete(Document).where(Document.source_id == source_id))
    return timeline_drafts


async def _remove_graph_support(
    session: AsyncSession, *, document_id: UUID | None = None, source_id: UUID | None = None,
    replay_source_id: UUID | None = None,
) -> list[ReplayDraft]:
    """Remove evidence-backed graph and timeline support in source/document → entities → relationships → events order.

    ``document_id`` and ``source_id`` select exactly one cleanup scope.
    Document deletion separately passes its locked source identity for the
    timeline collection invalidation after the document row is deleted.
    """
    if (document_id is None) == (source_id is None):
        raise ValueError("Specify one document or source for graph cleanup")
    if replay_source_id is not None and document_id is None:
        raise ValueError("A replay source identity is valid only for document cleanup")
    from modules.knowledge.entities import public as entities
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    refs = await list_evidence_ref_keys(session, document_id=document_id, source_id=source_id)
    membership_ids, entity_ids = await entities.support_cleanup_ids(
        session, document_id=document_id, source_id=source_id
    )
    relationship_ids, relationship_entity_ids = await relationships.support_cleanup_ids(
        session, refs=refs, document_id=document_id, source_id=source_id, membership_ids=membership_ids
    )
    timeline_entity_ids, timeline_event_ids = await timeline.support_cleanup_ids(
        session, document_id=document_id, source_id=source_id
    )
    all_entity_ids = sorted(set(entity_ids) | set(relationship_entity_ids) | set(timeline_entity_ids), key=str)
    # Lock entity rows before relationship rows consistently with correction transactions.
    await entities.lock_entity_ids(session, all_entity_ids)
    await relationships.lock_relationship_ids(session, relationship_ids)
    # Keep the cross-module lock order stable: event locks follow all graph locks.
    await timeline.lock_event_ids(session, timeline_event_ids)
    # Capture detached graph cleanup before any evidence/source cascade; this helper
    # performs no provider work and shares the caller's canonical deletion commit.
    await temporal.tombstone_scope(session, document_id=document_id, source_id=source_id)
    await relationships.purge_history_support(session, refs)
    if document_id is not None:
        await relationships.remove_document_support(
            session, document_id=document_id, refs=refs, membership_ids=membership_ids
        )
        await entities.remove_document_support(session, document_id)
        cleanup_source_id = replay_source_id
        if cleanup_source_id is None:
            cleanup_source_id = await session.scalar(select(Document.source_id).where(Document.id == document_id))
        if cleanup_source_id is None:
            raise ValueError("Document support cleanup requires its locked source identity")
        timeline_drafts = await timeline.remove_document_support(session, document_id=document_id, source_id=cleanup_source_id)
    else:
        assert source_id is not None  # document_id is None only for source-scoped cleanup
        await relationships.remove_source_support(
            session, source_id=source_id, refs=refs, membership_ids=membership_ids
        )
        await entities.remove_source_support(session, source_id)
        timeline_drafts = await timeline.remove_source_support(session, source_id=source_id)
    return timeline_drafts


async def append_content(
    session: AsyncSession, document_id: UUID, expected_version: int, content: str
) -> Document | None:
    """Append a version when the active source and expected revision permit it.

    Returns None for a missing document/source or inactive source. Identical
    content returns the current document before checking ``expected_version``,
    making a same-content retry a no-op even when its revision hint is stale;
    changed content with a stale revision raises ValueError. New versions commit
    through the realtime replay helper.
    """
    source_id = await session.scalar(select(Document.source_id).where(Document.id == document_id))
    if source_id is None:
        return None
    source = await sources.lock_source(session, source_id)
    if source is None or source.status != "active":
        return None
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        return None
    current = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == document.current_version,
        )
    )
    if current is None:
        raise RuntimeError("Current document version is missing")
    if current.content == content:
        return document
    # Check the revision only after the identical-content no-op to keep retries idempotent.
    if document.current_version != expected_version:
        raise ValueError("Document revision is stale")
    max_number = await session.scalar(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document.id)
    )
    next_version = int(max_number or 0) + 1
    digest = content_hash(content)
    version = DocumentVersion(
            document_id=document_id,
            version_number=next_version,
            content=content,
            content_hash=digest,
        )
    session.add(version)
    if await add_content_chunks(session, version):
        await _publish_document_ready(session, document, version)
    document.current_version = next_version
    document.content_hash = digest
    await commit_with_replay(
        session,
        [make_knowledge_change(document.source_id, document.id, next_version)],
    )
    await session.refresh(document)
    return document


async def read_extraction_input(
    session: AsyncSession, version_id: UUID, allowed_chunk_ids: list[UUID] | None = None
) -> ExtractionInput | None:
    """Return chunks for the active source's ready current version within extraction bounds.

    Raise ExtractionInputLimitError for empty, oversized or over-count input;
    unrelated selection validation remains ValueError. No partial input is returned.
    """
    statement = (
        select(
            Document.id, Document.source_id, Source.generation, Source.local_only,
            DocumentVersion.id, DocumentVersion.observed_at,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentVersion.id == version_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
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
        if not allowed_chunk_ids or len(allowed_chunk_ids) > EXTRACTION_CHUNK_LIMIT or len(set(allowed_chunk_ids)) != len(allowed_chunk_ids):
            raise ValueError("Extraction chunk IDs must be unique and bounded")
        chunks_query = chunks_query.where(DocumentChunk.id.in_(allowed_chunk_ids))
    stats = await session.execute(
        select(func.count(DocumentChunk.id), func.coalesce(func.sum(func.octet_length(DocumentChunk.content)), 0))
        .where(DocumentChunk.document_version_id == actual_version_id)
        .where(DocumentChunk.id.in_(allowed_chunk_ids) if allowed_chunk_ids is not None else true())
    )
    chunk_count, byte_count = cast("tuple[int, int]", tuple(stats.one()))
    if not chunk_count or chunk_count > EXTRACTION_CHUNK_LIMIT or byte_count > EXTRACTION_INPUT_BYTES:
        raise ExtractionInputLimitError("Extraction input exceeds its chunk or byte limit")
    chunks = list((await session.execute(chunks_query.order_by(DocumentChunk.chunk_index))).all())
    if allowed_chunk_ids is not None and {identifier for identifier, _ in chunks} != set(allowed_chunk_ids):
        return None
    return ExtractionInput(
        document_id=document_id, document_version_id=actual_version_id, source_id=source_id,
        source_generation=source_generation, local_only=local_only,
        observed_at=observed_at, chunks=tuple(ExtractionChunk(id=identifier, content=content) for identifier, content in chunks),
    )


async def get_first_chunk_id(session: AsyncSession, version_id: UUID) -> UUID | None:
    """Return the first chunk ID of a version without reading any chunk content.

    Deterministic provider mappers anchor evidence on the title chunk; unlike
    ``read_extraction_input`` this has no chunk-count or byte ceiling, so an oversized
    body cannot make a record unmappable.
    """
    return await session.scalar(
        select(DocumentChunk.id).where(DocumentChunk.document_version_id == version_id)
        .order_by(DocumentChunk.chunk_index).limit(1)
    )


async def read_extraction_evidence_refs(
    session: AsyncSession,
    *,
    document_id: UUID,
    document_version_id: UUID,
    source_id: UUID,
    source_generation: int,
    chunk_ids: list[UUID],
) -> list[ExtractionEvidenceRef] | None:
    """Validate a bounded set of current extraction chunks and return detached evidence refs."""
    if not chunk_ids or len(chunk_ids) > 150 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Extraction membership evidence must be nonempty and bounded")
    rows = (await session.execute(
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, DocumentChunk.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            Document.source_id == source_id,
            DocumentVersion.id == document_version_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active",
            Source.generation == source_generation,
            DocumentChunk.id.in_(chunk_ids),
        )
        .order_by(DocumentChunk.id)
    )).all()
    if len(rows) != len(chunk_ids):
        return None
    return [ExtractionEvidenceRef(
        document_id=row[0], document_version_id=row[1], source_id=row[2],
        source_generation=row[3], chunk_id=row[4],
    ) for row in rows]


async def list_ready_version_refs(
    session: AsyncSession, limit: int = 50, cursor: str | None = None
) -> tuple[list[ReadyVersionRef], str | None]:
    """Page through active-source current versions that have ready chunks."""
    if not 1 <= limit <= 100:
        raise ValueError("Ready-version page size must be between 1 and 100")
    statement = (
        select(
            Document.id, Document.created_at, Source.id, Source.generation,
            DocumentVersion.id, DocumentVersion.version_number, Source.local_only,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            DocumentVersion.content != "",
            Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )
    if cursor:
        created_at, identifier = decode_cursor(cursor)
        statement = statement.where(tuple_(Document.created_at, Document.id) < (created_at, identifier))
    rows = list((await session.execute(statement.order_by(desc(Document.created_at), desc(Document.id)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    result = [ReadyVersionRef(
        document_id=document_id, document_version_id=version_id, source_id=source_id,
        source_generation=generation, version_number=version_number, created_at=created_at,
        local_only=local_only,
    ) for document_id, created_at, source_id, generation, version_id, version_number, local_only in rows]
    return result, encode_cursor(rows[-1][1], rows[-1][0]) if more and rows else None


async def get_ready_version_ref(session: AsyncSession, version_id: UUID) -> ReadyVersionRef | None:
    """Resolve one version only while it remains the ready current version."""
    row = (await session.execute(
        select(
            Document.id, Document.created_at, Source.id, Source.generation,
            DocumentVersion.id, DocumentVersion.version_number, Source.local_only,
        )
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            DocumentVersion.id == version_id,
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            DocumentVersion.content != "",
            Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )).one_or_none()
    if row is None:
        return None
    document_id, created_at, source_id, generation, actual_version_id, version_number, local_only = row
    return ReadyVersionRef(
        document_id=document_id, document_version_id=actual_version_id, source_id=source_id,
        source_generation=generation, version_number=version_number,
        created_at=created_at, local_only=local_only,
    )


async def _publish_document_ready(session: AsyncSession, document: Document, version: DocumentVersion) -> None:
    """Queue independent entity and News events in the document transaction.

    The existing entity consumer commits its own single outbox record, so News
    receives a separate durable receipt instead of depending on that ACK order.
    Both payloads identify one immutable revision and source generation.
    """
    from core.events import DomainEvent
    from modules.ingestion import public as ingestion

    source = await session.get(Source, document.source_id)
    if source is None:
        return
    payload = {
        "source_id": str(source.id), "document_id": str(document.id),
        "document_version_id": str(version.id), "source_generation": source.generation,
        "version_number": version.version_number,
    }
    for event_type in ("document.version.ready", "news.document.ready"):
        await ingestion.publish_event(session, DomainEvent(
            id=uuid4(), type=event_type, version=1,
            occurred_at=datetime.now(UTC), producer="modules.knowledge.documents",
            payload=payload,
        ))


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
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    if not 1 <= version_number <= 2147483647:
        raise HTTPException(status_code=422, detail="Invalid cursor")
    return version_number


async def list_versions(
    session: AsyncSession, document_id: UUID, limit: int, cursor: str | None
) -> tuple[list[DocumentVersion] | None, str | None]:
    """List immutable revisions in ascending order; None indicates missing document."""
    after_version = decode_version_cursor(cursor) if cursor is not None else None
    if await session.get(Document, document_id) is None:
        return None, None
    statement = select(DocumentVersion).where(DocumentVersion.document_id == document_id)
    if after_version is not None:
        statement = statement.where(DocumentVersion.version_number > after_version)
    result = await session.scalars(
        statement.order_by(DocumentVersion.version_number).limit(limit + 1)
    )
    rows = list(result.all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_version_cursor(rows[-1].version_number) if has_more and rows else None
    return rows, next_cursor


async def get_version(
    session: AsyncSession, document_id: UUID, number: int
) -> DocumentVersion | None:
    """Fetch one immutable revision by document ID and version number."""
    return await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == number,
        )
    )


async def read_evidence_refs(
    session: AsyncSession, refs: list[tuple[UUID, UUID]], *, for_write: bool = False
) -> list[EvidenceReferenceRead]:
    """Resolve unique bounded version/chunk references, locking owners for writes."""
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    result = await _read_evidence_ref_rows(session, refs)
    if for_write:
        from modules.sources import public as sources_public

        for source_id in sorted({item.source_id for item in result}, key=str):
            if await sources_public.lock_source(session, source_id) is None:
                raise ValueError("Evidence source no longer exists")
        document_ids = sorted({item.document_id for item in result}, key=str)
        await session.scalars(
            select(Document)
            .where(Document.id.in_(document_ids))
            .order_by(Document.id)
            .with_for_update()
        )
        result = await _read_evidence_ref_rows(session, refs)
    return result


async def review_version_locator(session: AsyncSession, version_id: UUID) -> tuple[UUID, UUID] | None:
    """Return the owning document/source IDs for a retained review version."""
    row = (await session.execute(
        select(Document.id, Document.source_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == version_id)
    )).one_or_none()
    return (row[0], row[1]) if row else None


async def cleanup_evidence_version_document(session: AsyncSession, version_id: UUID) -> UUID | None:
    """Return the Document that owned a version, from the retained cleanup evidence of a deleted Document.

    Fallback for callers whose live ``review_version_locator`` no longer resolves a version. Reads only
    immutable receipt references (no lock), so it cannot invert the privacy -> receipt -> owner order.
    """
    return await session.scalar(
        select(DocumentCleanupOperation.document_id)
        .join(DocumentCleanupEvidenceReference, DocumentCleanupEvidenceReference.operation_id == DocumentCleanupOperation.id)
        .where(
            DocumentCleanupEvidenceReference.document_version_id == version_id,
            DocumentCleanupEvidenceReference.reference_kind == "version",
        )
        .limit(1)
    )


async def review_version_fences(
    session: AsyncSession, version_ids: list[UUID],
) -> dict[UUID, ReviewVersionFence]:
    """Return source-generation snapshots for a bounded de-duplicated version set."""
    ids = list(dict.fromkeys(version_ids))
    if len(ids) > 100:
        raise ValueError("Review version fence set exceeds its page limit")
    if not ids:
        return {}
    rows = (await session.execute(
        select(DocumentVersion.id, Document.id, Document.source_id, Source.generation, Source.name, DocumentVersion.version_number)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentVersion.id.in_(ids))
    )).all()
    return {
        version_id: ReviewVersionFence(document_id, source_id, generation, source_name, version_number)
        for version_id, document_id, source_id, generation, source_name, version_number in rows
    }


async def lock_review_version_evidence(
    session: AsyncSession, *, document_id: UUID, source_id: UUID, version_id: UUID,
    source_generation: int, chunk_ids: list[UUID],
) -> list[ReviewEvidenceRef] | None:
    """Fence a bounded owner correction to retained immutable evidence, including history."""
    if not chunk_ids or len(chunk_ids) > 5 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Review evidence must contain unique bounded chunks")
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id)
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
    refs = await read_evidence_refs(session, [(version_id, chunk_id) for chunk_id in chunk_ids])
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


async def lock_document_ids(session: AsyncSession, document_ids: list[UUID]) -> list[UUID]:
    """Lock a bounded, sorted set of retained document rows for owner transactions."""
    ids = sorted(set(document_ids), key=str)
    if len(ids) > 100:
        raise ValueError("Document lock set exceeds its atomic limit")
    if not ids:
        return []
    locked = list((await session.scalars(
        select(Document.id).where(Document.id.in_(ids)).order_by(Document.id).with_for_update()
    )).all())
    return locked


async def _read_evidence_ref_rows(
    session: AsyncSession, refs: list[tuple[UUID, UUID]]
) -> list[EvidenceReferenceRead]:
    """Build fresh ordered evidence DTOs with version provenance and exact reference validation.

    populate_existing prevents a caller's stale identity-map objects from authorizing a deleted,
    replaced, or edited source citation during backup finalization or other evidence-bound reads.
    """
    rows = (await session.execute(
        select(Document, DocumentVersion, DocumentChunk, Source.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs))
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
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if selection_fences is not None and not await validate_gadget_document_selection_fences(
        session, selection_fences,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")

    statement = (
        select(Document, DocumentVersion, DocumentChunk, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs))
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

    provenance_rows = (await session.scalars(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_version_id.in_(
                {version.id for _, version, _, _ in rows}
            )
        )
    )).all()
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


async def lock_chat_evidence_chunks(
    session: AsyncSession,
    refs: list[tuple[UUID, UUID]],
    *,
    require_active_source: bool = True,
    require_current_version: bool = False,
    selection_fences: tuple[GadgetDocumentSelectionFence, ...] | None = None,
) -> list[ChatEvidenceChunk]:
    """Hold key-share locks on exact evidence through Chat's short publication transaction.

    Lock order matches Documents deletion and append: Source, Document, immutable version,
    then chunk. A hard delete cannot commit between this current-evidence check and the caller's
    publication commit; callers must release these locks promptly by committing or rolling back.
    """
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if selection_fences is not None and not await validate_gadget_document_selection_fences(
        session, selection_fences,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")
    ref_filter = tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs)
    source_ids = list((await session.scalars(
        select(Source.id).join(Document, Document.source_id == Source.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .where(ref_filter).distinct().order_by(Source.id)
    )).all())
    if source_ids:
        await session.scalars(
            select(Source.id).where(Source.id.in_(source_ids)).order_by(Source.id)
            .with_for_update(read=True, key_share=True, of=Source)
        )
    document_ids = list((await session.scalars(
        select(Document.id).join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .where(ref_filter).distinct().order_by(Document.id)
    )).all())
    if document_ids:
        await session.scalars(
            select(Document.id).where(Document.id.in_(document_ids)).order_by(Document.id)
            .with_for_update(read=True, key_share=True, of=Document)
        )
    version_ids = list((await session.scalars(
        select(DocumentVersion.id).join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .where(ref_filter).distinct().order_by(DocumentVersion.id)
    )).all())
    if version_ids:
        await session.scalars(
            select(DocumentVersion.id).where(DocumentVersion.id.in_(version_ids)).order_by(DocumentVersion.id)
            .with_for_update(read=True, key_share=True, of=DocumentVersion)
        )
    chunk_ids = [chunk_id for _, chunk_id in refs]
    await session.scalars(
        select(DocumentChunk.id).where(DocumentChunk.id.in_(chunk_ids)).order_by(DocumentChunk.id)
        .with_for_update(read=True, key_share=True, of=DocumentChunk)
    )
    evidence = await read_chat_evidence_chunks(
        session, refs, require_active_source=require_active_source,
        require_current_version=require_current_version,
    )
    if selection_fences is not None and len(evidence) != len(refs):
        raise ValueError("One or more exact selected evidence chunks are unavailable")
    return evidence

