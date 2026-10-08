import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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

from core.chunking import chunk_text
from core.events import DomainEvent
from core.pagination import decode_cursor, encode_cursor
from core.realtime import ReplayDraft, commit_with_replay, make_knowledge_change
from core.tools.schemas import ToolDestination, ToolOutputFence
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
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
    NormalizedDocumentKeyState,
    NormalizedDocumentPreparation,
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
from modules.sources.schemas import ConnectorSource, SourceExportFence, SourceFence

if TYPE_CHECKING:
    from modules.connectors.public import ProviderScopeSnapshot

_log = logging.getLogger(__name__)


async def observability_quality_summary(
    session: AsyncSession, *, instance_operator: bool,
) -> dict[str, int]:
    """Return global document counts only to an explicitly admitted instance operator."""
    if instance_operator is not True:
        raise HTTPException(status_code=403, detail="Instance operator required")
    document_count = int(await session.scalar(select(func.count()).select_from(Document)) or 0)
    orphan_chunks = int(await session.scalar(select(func.count()).select_from(DocumentChunk).outerjoin(
        DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id,
    ).where(DocumentVersion.id.is_(None))) or 0)
    return {"document_count": document_count, "orphan_chunks": orphan_chunks}

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "EvidenceReferenceRead",
    "NormalizedDocumentKeyState",
    "NormalizedDocumentPreparation",
    "NormalizedDocumentValidationRejected",
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
    owner_id: int, workspace_id: UUID, record_kind: str, snapshot_at: datetime,
    position_at: datetime, position_id: UUID, access_fence: AccessFence,
) -> str:
    """Encode a canonical export cursor bound to owner, filters, cutoff and admission revisions."""
    if owner_id != access_fence.user_id or workspace_id != access_fence.workspace_id:
        raise ValueError("Document export cursor identity does not match its access fence")
    payload = {
        "v": 3, "owner": owner_id, "workspace": str(workspace_id), "kind": record_kind,
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "snapshot": snapshot_at.astimezone(UTC).isoformat(),
        "at": position_at.astimezone(UTC).isoformat(), "id": str(position_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_document_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID, record_kind: str, access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Decode a canonical cursor and reject owner, filter, or admission-revision changes."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid document export cursor")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "v", "owner", "workspace", "kind", "membership_revision", "configuration_revision",
            "snapshot", "at", "id",
        }:
            raise ValueError("Invalid document export cursor")
        if (payload["v"] != 3 or payload["owner"] != owner_id
                or payload["workspace"] != str(workspace_id) or payload["kind"] != record_kind
                or owner_id != access_fence.user_id or workspace_id != access_fence.workspace_id
                or payload["membership_revision"] != access_fence.membership_revision
                or payload["configuration_revision"] != access_fence.configuration_revision):
            raise ValueError("Document export cursor belongs to another owner or record kind")
        snapshot_at = datetime.fromisoformat(payload["snapshot"])
        position_at = datetime.fromisoformat(payload["at"])
        if any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, position_at)):
            raise ValueError("Document export cursor timestamps must be timezone-aware")
        snapshot_at, position_at = snapshot_at.astimezone(UTC), position_at.astimezone(UTC)
        if snapshot_at > datetime.now(UTC):
            raise ValueError("Document export cursor cutoff cannot be in the future")
        position_id = UUID(payload["id"])
        if _encode_document_export_cursor(
            owner_id, workspace_id, record_kind, snapshot_at, position_at, position_id, access_fence,
        ) != cursor:
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


async def _require_document_export_owner(
    session: AsyncSession, owner_id: int, *, scope: Scope, multi_workspace_enabled: bool,
) -> AccessFence:
    """Admit the explicit owner scope before projecting source-owned documents."""
    actor_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    if owner_id != actor_id:
        raise ValueError("Document export actor does not match the admitted scope")
    return await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _document_export_scope(snapshot_at: datetime) -> tuple[ColumnElement[bool], ...]:
    """Select retained documents that existed and were unchanged at the page cutoff."""
    return Document.created_at <= snapshot_at, Document.updated_at <= snapshot_at


async def _document_export_count(
    session: AsyncSession, record_kind: str, snapshot_at: datetime, *, scope: Scope,
) -> int:
    """Count owner-visible rows at the fixed cutoff so callers can detect export drift."""
    if record_kind == "documents":
        statement = select(func.count()).select_from(Document).join(Source, Source.id == Document.source_id)
        statement = statement.where(
            Document.workspace_id == scope.workspace_id,
            *_document_export_scope(snapshot_at),
            Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
        )
    else:
        statement = (
            select(func.count()).select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.workspace_id == scope.workspace_id,
                *_document_export_scope(snapshot_at), DocumentVersion.created_at <= snapshot_at,
                Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
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
    scope: Scope,
    multi_workspace_enabled: bool,
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
    access_fence = await _require_document_export_owner(
        session, owner_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if cursor is None:
        snapshot_at = datetime.now(UTC)
        position = None
    else:
        snapshot_at, position_at, position_id = _decode_document_export_cursor(
            cursor, owner_id, scope.workspace_id, record_kind, access_fence,
        )
        position = (position_at, position_id)
    snapshot_count = await _document_export_count(session, record_kind, snapshot_at, scope=scope)
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
                Document.workspace_id == scope.workspace_id,
                *_document_export_scope(snapshot_at),
                Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
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
            _encode_document_export_cursor(
                owner_id, scope.workspace_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id,
                access_fence,
            )
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
                Document.workspace_id == scope.workspace_id,
                *_document_export_scope(snapshot_at), DocumentVersion.created_at <= snapshot_at,
                Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
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
            _encode_document_export_cursor(
                owner_id, scope.workspace_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id,
                access_fence,
            )
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
    scope: Scope,
    multi_workspace_enabled: bool,
) -> DocumentExportFenceValidation:
    """Recheck bounded document identity, source generation, deletion, and count before publication."""
    if record_kind not in {"documents", "versions"} or not 0 <= expected_snapshot_count <= 2**63 - 1:
        raise ValueError("Document export revalidation input is invalid")
    if len(fences) > 100:
        raise ValueError("Document export revalidation is limited to 100 records")
    actor_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    if owner_id != actor_id:
        raise ValueError("Document export actor does not match the admitted scope")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source_generations: dict[UUID, int] = {}
    for fence in fences:
        previous = source_generations.setdefault(fence.source_id, fence.current_source_generation)
        if previous != fence.current_source_generation:
            observed_count = await _document_export_count(session, record_kind, snapshot_at, scope=scope)
            return DocumentExportFenceValidation(
                valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count,
            )
    source_fences = [
        SourceExportFence(source_id=source_id, workspace_id=scope.workspace_id, generation=generation)
        for source_id, generation in source_generations.items()
    ]
    eligible_source_ids = set(await sources.filter_export_eligible_sources(
        session, source_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ))
    if len(eligible_source_ids) != len(source_fences):
        observed_count = await _document_export_count(session, record_kind, snapshot_at, scope=scope)
        return DocumentExportFenceValidation(
            valid=False, reason="source_generation_changed", observed_snapshot_count=observed_count,
        )
    observed_count = await _document_export_count(session, record_kind, snapshot_at, scope=scope)
    if observed_count != expected_snapshot_count:
        return DocumentExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed_count)
    for fence in fences:
        row = (await session.execute(
            select(Document.created_at, Document.updated_at, Document.current_version,
                   Source.id.label("source_id"), Source.status, Source.generation)
            .join(Source, Source.id == Document.source_id)
            .where(Document.id == fence.document_id, Document.workspace_id == scope.workspace_id)
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
    *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, int]:
    """Return current evidence revisions backed by matching document and provider fences.

    Documents owns version/provenance reads. The result contains accepted observation IDs
    paired with their current version numbers; foreign modules never receive Document ORM
    rows or provenance bodies.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
        .where(
            DocumentVersion.id.in_({item.document_version_id for item in candidates}),
            *_document_scope(scope),
        )
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
    *, scope: Scope, multi_workspace_enabled: bool,
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
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
          Source.status.in_(("active", "paused", "archived")),
          Source.id.in_(sources.export_eligible_source_ids(scope=scope)),
          Document.workspace_id == scope.workspace_id,
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
    session: AsyncSession, candidate: TimelineExportEvidenceCandidate, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> TimelineExportEvidenceRead | None:
    """Prove a retained exact chunk and accepted generation, while separately fencing its current source.

    Timeline facts may retain support from an earlier source generation after a source is paused or
    archived. The source owner's purge eligibility and fresh scalar projection keep the evidence usable
    only while its exact source/document/version/chunk and accepted provenance remain retained.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
          Source.id.in_(sources.export_eligible_source_ids(scope=scope)),
          Document.workspace_id == scope.workspace_id,
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
    session: AsyncSession, version_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[ProviderDocumentSnapshotRead]:
    """Read exact immutable provider versions for an already owner-authenticated route.

    This query is intentionally absent from agent, tool, and collector entry points;
    a future agent surface must apply P07 grants before calling a suitable owner API.
    Missing, deleted, archived, or duplicate versions fail as a whole request.
    """
    if not 1 <= len(version_ids) <= 100 or len(version_ids) != len(set(version_ids)):
        raise ValueError("version_ids must contain 1 to 100 unique values")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = list((await session.execute(
        select(Document, DocumentVersion, Source, NormalizedVersionProvenance)
        .select_from(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(DocumentVersion.id.in_(version_ids), Source.provider.in_(PROVIDER_IDS),
               *_document_scope(scope))
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
    scope: Scope,
    multi_workspace_enabled: bool,
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
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
            *_document_scope(scope),
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
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_source_generation: int | None = None,
) -> NewsDocumentProjection | None:
    """Return a current, ready document projection only under its active source generation.

    The caller must first authorize the source through the Sources public contract.
    This read excludes deleted, paused, replaced, or incomplete versions and caps
    chunks at 100. Legacy/manual documents remain readable with explicitly
    non-snapshot metadata provenance rather than inferred historical metadata.
    """
    components = await _current_document_components(
        session, document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_source_generation=expected_source_generation,
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
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    expected_source_generation: int | None = None, admitted: bool = False,
) -> tuple[Document, DocumentVersion, Source, NormalizedVersionProvenance | None] | None:
    """Resolve one active current revision and verify its accepted provider scope without reading chunks.

    The Documents owner uses this shared gate for content projections and exact selections so a
    normalized record cannot inherit a new source generation or mutable provider configuration.
    """
    if not admitted:
        await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(Document, DocumentVersion, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            *_document_scope(scope),
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
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
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
    scope: Scope, multi_workspace_enabled: bool, lock_rows: bool = True, max_documents: int = 32,
) -> bool:
    """Revalidate exact selected versions and provider policy before content or remote use.

    Selection count, source count, and lock order are bounded. The original admitted fence is
    compared while Source rows are share-locked in UUID order before document rows; accepted
    provenance is then checked against live provider scope. Locks last through caller transaction end.
    """
    access_fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await _validate_gadget_document_selection_fences_admitted(
        session, fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence, lock_rows=lock_rows, max_documents=max_documents,
    )


async def news_retained_observation_allowed(
    session: AsyncSession, *, document_id: UUID, source_id: UUID,
    expected_source_generation: int, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Confirm a retained News count still belongs to the current active source scope.

    This boolean fence deliberately does not return historical or current text and
    does not require the observed historical version to remain current. It checks
    the document's present ready revision, active source generation, and current
    provider scope without loading chunks or exposing metadata to News.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
            *_document_scope(scope),
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
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_scope is not None and current_scope.discriminator == accepted_scope


async def news_projection_scope_unavailable(
    session: AsyncSession, document_id: UUID, expected_source_generation: int, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Report only whether a supported provider scope fence prevents News evidence output.

    The result contains no source configuration, item identifiers, or counts. Deleted,
    replaced, inactive, or stale-generation documents are not classified as scope failures.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = (await session.execute(
        select(Document, DocumentVersion, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.id == document_id,
            *_document_scope(scope),
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

    current_scope = await connectors.get_current_provider_scope(
        session, source.id, source.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return current_scope is None or accepted_scope != current_scope.discriminator


async def news_current_scope_status(
    session: AsyncSession, source_ids: tuple[UUID, ...], *,
    scope: Scope, multi_workspace_enabled: bool,
) -> NewsProjectionStatus:
    """Summarize current provider-scope omissions for at most 100 selected documents.

    Only fixed reason codes leave Documents; provider settings, credentials, item IDs and
    omission counts stay inside the owner module. A capped scan reports incompleteness.
    """
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids):
        raise ValueError("News scope status requires 1 to 32 unique sources")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
            *_document_scope(scope),
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

            snapshot = await connectors.get_current_provider_scope(
                session, source_id, generation, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
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
    channel_ids: tuple[str, ...] | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[list[NewsDocumentProjection], str | None]:
    """Page bounded current ready versions from explicitly authorized active sources."""
    if not source_ids or len(source_ids) > 32 or len(set(source_ids)) != len(source_ids) or not 1 <= limit <= 100:
        raise ValueError("News source page must contain 1 to 32 unique sources and a bounded limit")
    access_fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if channel_ids is not None and (
        len(channel_ids) > 32 or len(set(channel_ids)) != len(channel_ids)
        or any(re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in channel_ids)
    ):
        raise ValueError("Channel scope must contain at most 32 unique numeric identifiers")
    fingerprint = _news_projection_cursor_fingerprint(
        access_fence=access_fence, source_ids=source_ids,
        observed_since=observed_since, channel_ids=channel_ids,
    )
    statement = (
        select(Document.id, Document.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids), Document.current_version == DocumentVersion.version_number,
            *_document_scope(scope),
            Document.extraction_status.in_(("ready", "succeeded")), Source.status == "active",
            select(DocumentChunk.id).where(DocumentChunk.document_version_id == DocumentVersion.id).exists(),
        )
    )
    if observed_since is not None:
        statement = statement.where(func.coalesce(Document.observed_at, DocumentVersion.observed_at) >= observed_since)
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
        created_at, document_cursor = _decode_news_projection_cursor(cursor, fingerprint)
        statement = statement.where(tuple_(Document.created_at, Document.id) < (created_at, document_cursor))
    rows = list((await session.execute(statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    projections = []
    for document_id, _created_at in rows:
        item = await get_news_document_projection(
            session, document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
        if item is not None and item.source_id in source_ids:
            if channel_ids is not None and (
                item.provider_metadata is None
                or item.provider_metadata.provider != "telegram"
                or item.provider_metadata.telegram is None
                or item.provider_metadata.telegram.channel_id not in channel_ids
            ):
                continue
            projections.append(item)
    next_cursor = _encode_news_projection_cursor(
        rows[-1][1], rows[-1][0], fingerprint,
    ) if more and rows else None
    return projections, next_cursor


async def list_gadget_document_projections(
    session: AsyncSession, *, source_ids: tuple[UUID, ...], limit: int = 50,
    cursor: str | None = None, channel_ids: tuple[str, ...] | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> GadgetDocumentProjectionList:
    """Return active, current, ready source records as a small dashboard projection page.

    Interaction state is read for the admitted actor. Documents retains source-generation and
    provider-scope validation; only short excerpts and typed provider fields leave this boundary.
    Full text stays out of dashboard APIs.
    """
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    actor_id = access_fence.user_id
    projections, next_cursor = await list_news_document_projections(
        session, source_ids=source_ids, limit=limit, cursor=cursor,
        channel_ids=channel_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    version_ids = [item.document_version_id for item in projections]
    interaction_rows = (await session.scalars(
        select(DocumentInteraction).where(
            DocumentInteraction.owner_id == actor_id,
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
    scope: Scope, multi_workspace_enabled: bool,
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
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = (
        select(Document.id, DocumentVersion.id, DocumentVersion.created_at)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.source_id.in_(source_ids),
            *_document_scope(scope),
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
        projection = await get_news_document_projection(
            session, document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
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
    session: AsyncSession, *, document_id: UUID, version_number: int,
    payload: GadgetDocumentInteractionPatch, scope: Scope, multi_workspace_enabled: bool,
) -> GadgetDocumentInteractionRead | None:
    """Persist exact-version state for the admitted actor and publish under the original access fence."""
    access_fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    actor_id = access_fence.user_id
    source_id = await session.scalar(select(Document.source_id).where(
        Document.id == document_id, *_document_scope(scope),
    ))
    if source_id is None:
        return None
    await sources.lock_source_for_document(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
    )
    document = await session.scalar(
        select(Document).where(Document.id == document_id, *_document_scope(scope))
        .with_for_update().execution_options(populate_existing=True)
    )
    if document is None:
        return None
    projection = await get_news_document_projection(
        session, document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if projection is None or projection.version_number != version_number:
        return None
    row = await session.get(
        DocumentInteraction, (actor_id, projection.document_version_id),
    )
    now = datetime.now(UTC)
    read_at = (now if payload.read else None) if payload.read is not None else (row.read_at if row else None)
    bookmarked_at = (now if payload.bookmarked else None) if payload.bookmarked is not None else (row.bookmarked_at if row else None)
    if read_at is None and bookmarked_at is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        row = DocumentInteraction(
            owner_id=actor_id, document_version_id=projection.document_version_id,
            read_at=read_at, bookmarked_at=bookmarked_at,
        )
        session.add(row)
    else:
        row.read_at = read_at
        row.bookmarked_at = bookmarked_at
        row.updated_at = now
    await commit_with_replay(
        session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    return GadgetDocumentInteractionRead(
        document_version_id=projection.document_version_id,
        read_at=read_at, bookmarked_at=bookmarked_at,
    )


def _news_projection_cursor_fingerprint(
    *, access_fence: AccessFence, source_ids: tuple[UUID, ...], observed_since: datetime | None,
    channel_ids: tuple[str, ...] | None,
) -> str:
    """Hash News filters with the admitted actor/workspace and membership/configuration revisions."""
    context = {
        "workspace": str(access_fence.workspace_id),
        "actor": access_fence.user_id,
        "membership_revision": access_fence.membership_revision,
        "configuration_revision": access_fence.configuration_revision,
        "sources": sorted(str(item) for item in source_ids),
        "observed_since": observed_since.astimezone(UTC).isoformat() if observed_since else None,
        "channels": sorted(channel_ids) if channel_ids is not None else None,
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _encode_news_projection_cursor(created_at: datetime, document_id: UUID, fingerprint: str) -> str:
    """Encode the bounded keyset and its exact owner/source/filter context."""
    raw = json.dumps([created_at.astimezone(UTC).isoformat(), str(document_id), fingerprint], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_news_projection_cursor(cursor: str, fingerprint: str) -> tuple[datetime, UUID]:
    """Decode a canonical projection cursor bound to its owner/source/filter context."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        timestamp, identifier, actual_fingerprint = json.loads(raw)
        parsed = datetime.fromisoformat(timestamp)
        parsed_id = UUID(identifier)
        if (
            parsed.tzinfo is None or actual_fingerprint != fingerprint
            or _encode_news_projection_cursor(parsed, parsed_id, fingerprint) != cursor
        ):
            raise ValueError
        return parsed, parsed_id
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


async def ensure_demo_article(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int, int]:
    """Normalize one fictional article through Documents and preserve provenance and chunks.

    Returns (created, existing, skipped). The stable source/provider identity and accepted content
    hash make retries idempotent; a tombstoned normalized identity stays deleted. The caller owns
    the transaction and receipt, while Documents owns source fencing, normalization, and chunking.
    The finite caller supplies real scope/configured flag; no demo owner/default epoch is inferred.
    """
    from core.demo_seed import P12_DEMO_NAMESPACE, p12_demo_seed_id

    article_id = p12_demo_seed_id("article", "lantern-inscription-care")
    source_id = p12_demo_seed_id("article", "source")
    content = (
        "Fictional field note: Mira records that the north orchard lantern inscriptions should be "
        "photographed in soft morning light before the catalogue is assembled."
    )
    await sources.ensure_demo_source(session, source_id, P12_DEMO_NAMESPACE,
                                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await sources.get_source_fence(session, source_id, scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled)
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
    ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
    drafts = chunk_text(version.content)
    for index, draft in enumerate(drafts):
        session.add(DocumentChunk(
            document_version_id=version.id, chunk_index=index, content=draft.content,
            content_hash=content_hash(draft.content), token_count=draft.token_count,
            metadata_json=draft.metadata,
        ))
    return len(drafts)


async def backfill_current_chunks(session: AsyncSession, limit: int = 2, *, multi_workspace_enabled: bool) -> int:
    """Fill legacy manual revisions created before chunking was enabled.

    System path: each version's Source resolves its own internal job scope, then admission,
    Source and Document locks are taken in order and held through publish and commit. One
    transaction per version, so no two workspaces' locks are ever held together. A version
    whose lineage is denied or stale is skipped; nothing is rebased onto another actor.
    """
    candidates = [(row.id, row.document_id, row.version_number) for row in (await session.scalars(
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
        .order_by(DocumentVersion.id).limit(limit * 10)  # bounded window so denied lineages cannot starve others
    )).all()]
    await session.rollback()
    done = 0
    for version_id, document_id, version_number in candidates:
        if done >= limit:
            break
        try:
            hint = await session.get(Document, document_id)
            scope = (await sources.resolve_source_job_scope(
                session, hint.source_id, multi_workspace_enabled=multi_workspace_enabled,
            )) if hint else None
            if hint is None or scope is None:
                await session.rollback()
                _log.warning("chunk backfill skipped version %s: no admissible scope", version_id)
                continue
            access_fence = await read_access_fence(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            source = await sources.lock_source(
                session, hint.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=access_fence,
            )
            document = await session.scalar(
                select(Document).where(Document.id == document_id,
                                       Document.workspace_id == scope.workspace_id).with_for_update()
            )
            version = await session.get(DocumentVersion, version_id)
            if (
                source is None or source.status != "active" or document is None or version is None
                or document.current_version != version_number or version.version_number != version_number
            ):
                await session.rollback()
                _log.warning("chunk backfill skipped version %s: stale lineage", version_id)
                continue
            if await add_content_chunks(session, version):
                await _publish_document_ready(
                    session, document, version, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    access_fence=access_fence, source_fence=source,
                )
            await session.commit()
            done += 1
        except HTTPException as exc:
            await session.rollback()
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            _log.warning("chunk backfill skipped version %s: HTTP %s", version_id, exc.status_code)
    return done


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


def _require_document_owner(scope: Scope) -> None:
    """Require an explicit owner/internal subject; membership never grants shared content.

    Construction is not admission. Public callers must additionally revalidate the actual
    actor, membership and feature gate through the workspace owner before any query.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit Document scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


def _document_scope(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Constrain roots before paging to admitted workspace and Source-owned retained lineage.

    Paused and connector-only archived Sources remain readable. Queued/running/failed
    data purges hide their content; the Source projection grants no membership/resource
    authority and callers must first perform real owner admission. No Source ORM is read.
    """
    _require_document_owner(scope)
    return (
        Document.workspace_id == scope.workspace_id,
        Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )


async def _admit_document_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> AccessFence:
    """Revalidate actual owner/default membership with the explicit rollout gate, without locks.

    Members are denied before root IDs, counts or content. This snapshot is not HTTP-session
    or final-send proof; writers acquire their early Source/access set and routes retain
    exact authenticated-session locks through commit. No commit or external I/O occurs.
    """
    _require_document_owner(scope)
    return await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _read_document_source_id(
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Locate one visible root's Source before acquiring any domain lock, without metadata.

    Scope/Source lineage precedes the exact ID query. Missing, foreign and data-purged
    documents return None alike; writers then lock Source and freshly reload their own root.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(select(Document.source_id).where(Document.id == document_id, *_document_scope(scope)))


async def _assert_new_document_key(
    session: AsyncSession, source_id: UUID, external_id: str | None, *, scope: Scope,
) -> None:
    """Reject an occupied/tombstoned exact external key under the caller-held Source lock.

    Lock an existing canonical root before its normalized identity, retaining absence under
    Source serialization. Nullable manual keys have no shared namespace. No winner adoption,
    tombstone resurrection, placeholder, mutation, commit or foreign ORM occurs.
    """
    if external_id is None:
        return
    document = await session.scalar(select(Document).where(
        Document.source_id == source_id, Document.external_id == external_id,
    ).order_by(Document.id).with_for_update().execution_options(populate_existing=True))
    identity = await session.scalar(select(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.source_id == source_id, NormalizedDocumentIdentity.external_id == external_id,
    ).order_by(NormalizedDocumentIdentity.id).with_for_update().execution_options(populate_existing=True))
    if any(row is not None and row.workspace_id != scope.workspace_id for row in (document, identity)):
        raise RuntimeError("document_key_namespace_changed")
    if identity is not None and identity.tombstoned_at is not None:
        raise ValueError("Document identifier was previously deleted")
    if document is not None or identity is not None:
        raise ValueError("Document identifier already exists")


async def create_document(
    session: AsyncSession, payload: DocumentCreate, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document:
    """Create an owner workspace root/version under early Source/key locks and scoped replay.

    Active Source and real owner admission precede all writes. Reject occupied/tombstoned
    external keys; initial chunks and two ready events commit atomically with the root/replay.
    Return own ORM for this module's route only. No external I/O or cleanup authority change.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Manual document creation requires a workspace owner")
    _require_document_owner(scope)
    locked = await sources.lock_source_set(session, (payload.source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    source_fence, access_fence = locked.fences[0], locked.access_fence
    if source_fence.status != "active":
        raise ValueError("Cannot add documents to an inactive source")
    await _assert_new_document_key(session, payload.source_id, payload.external_id, scope=scope)
    digest = content_hash(payload.content)
    document = Document(
        workspace_id=scope.workspace_id,
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
            await _publish_document_ready(session, document, version, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
        # Load generated metadata under the original locks, before the final commit releases them.
        await session.flush()
        await session.refresh(document)
        await commit_with_replay(
            session,
            [make_knowledge_change(payload.source_id, document.id, 1, scope=scope)],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    except IntegrityError:
        await session.rollback()
        raise
    return document


async def document_metadata(
    session: AsyncSession, document_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, tuple[str, str | None]]:
    """Return ``{document_id: (title, mime_type)}`` for the given ids (metadata only, never content).

    Read-only batch projection for the automations producer sweep; callers pass at most one
    sweep page of ids. Unknown ids are simply absent.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(document_ids) > 100 or len(set(document_ids)) != len(document_ids):
        raise ValueError("Document metadata page must contain at most 100 unique IDs")
    rows = await session.execute(select(Document.id, Document.title, Document.mime_type).where(
        Document.id.in_(document_ids), *_document_scope(scope)).limit(100))
    return {row[0]: (row[1], row[2]) for row in rows.all()}


async def get_document(
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> Document | None:
    """Read one owned retained root after real admission, without granting member visibility.

    Workspace and Source-owned deletion lineage precede the ID query. Inactive retained
    metadata remains eligible except unfinished/failed data purges. Own ORM serves only
    Documents routes; external callers must migrate to detached owner projections.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(select(Document).where(
        Document.id == document_id, *_document_scope(scope),
    ).execution_options(populate_existing=True))


async def existing_document_ids(
    session: AsyncSession, document_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Return sorted IDs for at most 100 retained documents in the admitted owner workspace.

    This identifier-only helper is safe for cross-domain selection: SQL applies workspace and
    unfinished-purge fences before returning any ID. It neither locks rows nor reads content.
    """
    if len(document_ids) > 100 or any(type(identifier) is not UUID for identifier in document_ids):
        raise ValueError("Document identity batch exceeds 100 IDs")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not document_ids:
        return []
    rows = await session.scalars(select(Document.id).where(
        Document.id.in_(set(document_ids)), *_document_scope(scope),
    ).order_by(Document.id).limit(100))
    return list(rows.all())


async def list_indexable_workspace_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Discover at most 100 workspaces with ready current chunks for automatic indexing.

    Results are identity-only candidates, not authority. The Search worker must resolve the
    durable owner and perform current workspace/module/config/privacy admission before effects.
    No content, credentials, foreign workspace/settings ORM, commit or provider call is used.
    """
    if type(limit) is not int or limit < 1 or (after is not None and type(after) is not UUID):
        raise ValueError("Workspace discovery requires a positive limit and UUID cursor")
    statement = (
        select(Document.workspace_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False),
        )
    )
    if after is not None:
        statement = statement.where(Document.workspace_id > after)
    result = await session.scalars(
        statement.distinct().order_by(Document.workspace_id).limit(min(limit, 100)),
    )
    return tuple(result.all())


async def list_ready_document_workspace_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Discover bounded workspace identities with a ready current Document version.

    This is a worker candidate seam only. A returned workspace conveys no authority; the
    caller must resolve and admit its actual owner and module scope before any protected read
    or effect. The query exposes no content, chunks, Source, Workspace or account state.
    """
    if type(limit) is not int or limit < 1 or (after is not None and type(after) is not UUID):
        raise ValueError("Workspace discovery requires a positive limit and UUID cursor")
    statement = (
        select(Document.workspace_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
        )
    )
    if after is not None:
        statement = statement.where(Document.workspace_id > after)
    result = await session.scalars(
        statement.distinct().order_by(Document.workspace_id).limit(min(limit, 100)),
    )
    return tuple(result.all())


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


async def has_document_identity(
    session: AsyncSession, source_id: UUID, external_id: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Check one exact visible canonical key for upload dedup, exposing no metadata.

    Actual owner/internal admission and Source lineage precede the query. A deleted,
    foreign or data-purged key is False, never an adopted tombstone or authority upgrade.
    Upload caller retains the Source lock; this method acquires no lock or commit.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return bool(
        await session.scalar(
            select(Document.id).where(Document.source_id == source_id, Document.external_id == external_id,
                *_document_scope(scope)).limit(1)
        )
    )


async def _normalized_source_proof(
    session: AsyncSession, source_id: UUID, source_generation: int, *,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> ConnectorSource:
    """Freshly compare complete original access/Source fences without taking locks.

    Owner public reads enforce real owner/default-workspace admission and a bound
    internal Source generation. Callers retain earlier admission/Source locks;
    any stale, inactive or foreign fence aborts the whole transaction, with no
    membership/epoch rebasing, mutation, commit or remote I/O.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise RuntimeError("normalized_preparation_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    current_access = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (current_access != access_fence or current_source != source_fence
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or access_fence.workspace_id != scope.workspace_id
            or source_fence.status != "active" or source_fence.generation != source_generation):
        raise RuntimeError("normalized_preparation_fence_changed")
    projection = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (projection is None or projection.workspace_id != scope.workspace_id
            or projection.id != source_id or projection.status != "active"
            or projection.generation != source_generation):
        raise RuntimeError("normalized_preparation_source_changed")
    return projection


async def _normalized_key_states(
    session: AsyncSession, source_id: UUID, external_ids: tuple[str, ...], workspace_id: UUID,
) -> tuple[tuple[NormalizedDocumentKeyState, ...], dict[str, Document], dict[str, NormalizedDocumentIdentity]]:
    """Read at most32 exact namespaces freshly, without following foreign pointers.

    The Source is already admitted/held. Namespace corruption and a live identity
    whose canonical Document vanished are transaction conflicts. A normal
    non-normalized key collision is retained as comparison data for early input
    rejection. No locks or writes occur and no ORM leaves the Documents owner.
    """
    documents = {cast(str, row.external_id): row for row in (await session.scalars(select(Document).where(
        Document.source_id == source_id, Document.external_id.in_(external_ids),
    ).execution_options(populate_existing=True))).all()}
    identities = {row.external_id: row for row in (await session.scalars(select(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.source_id == source_id,
        NormalizedDocumentIdentity.external_id.in_(external_ids),
    ).execution_options(populate_existing=True))).all()}
    states = []
    for external_id in external_ids:
        document, identity = documents.get(external_id), identities.get(external_id)
        if any(row is not None and (row.workspace_id != workspace_id or row.source_id != source_id
                                    or row.external_id != external_id) for row in (document, identity)):
            raise RuntimeError("normalized_preparation_namespace_changed")
        if identity is not None:
            if identity.document_id is not None and (document is None or identity.document_id != document.id):
                raise RuntimeError("normalized_preparation_foreign_pointer")
            if identity.tombstoned_at is None and document is None:
                raise RuntimeError("normalized_preparation_canonical_missing")
        try:
            states.append(NormalizedDocumentKeyState(
                external_id=external_id, document_id=document.id if document is not None else None,
                normalized_identity_id=identity.id if identity is not None else None,
                identity_document_id=identity.document_id if identity is not None else None,
                tombstoned_at=identity.tombstoned_at if identity is not None else None,
            ))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("normalized_preparation_stored_shape_changed") from exc
    return tuple(states), documents, identities


async def prepare_normalized_document_keys(
    session: AsyncSession, source_id: UUID, external_ids: tuple[str, ...], *,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> NormalizedDocumentPreparation:
    """Prepare <=32 keys after Source and before Ingestion/Observation/outbox roots.

    Reject oversized input before querying, preserve spelling and deduplicate
    discovery keys only. Lock all discovered Documents by UUID, then all identity
    rows by UUID; compare freshly loaded mappings/absence/tombstones against
    discovery. A changed set aborts rather than acquiring another earlier root.
    Empty input is valid. No placeholders, mutation, commit or lock token exists.
    """
    if (not isinstance(source_id, UUID) or not isinstance(external_ids, tuple)
            or len(external_ids) > 32 or any(type(key) is not str or not 1 <= len(key) <= 512 for key in external_ids)):
        raise ValueError("Normalized preparation requires at most32 exact external keys")
    external_ids = tuple(dict.fromkeys(external_ids))
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("normalized_preparation_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    await _normalized_source_proof(
        session, source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    before, documents, identities = await _normalized_key_states(session, source_id, external_ids, scope.workspace_id)
    if documents:
        await session.scalars(select(Document).where(Document.id.in_([row.id for row in documents.values()]))
                              .order_by(Document.id).with_for_update().execution_options(populate_existing=True))
    if identities:
        await session.scalars(select(NormalizedDocumentIdentity).where(
            NormalizedDocumentIdentity.id.in_([row.id for row in identities.values()]),
        ).order_by(NormalizedDocumentIdentity.id).with_for_update().execution_options(populate_existing=True))
    after, _, _ = await _normalized_key_states(session, source_id, external_ids, scope.workspace_id)
    if before != after:
        raise RuntimeError("normalized_preparation_identity_changed")
    return NormalizedDocumentPreparation(workspace_id=scope.workspace_id, source_id=source_id,
                                         source_generation=source_fence.generation, keys=after)


async def _normalized_target(
    session: AsyncSession, external_id: str, preparation: NormalizedDocumentPreparation,
) -> tuple[NormalizedDocumentKeyState, Document | None, NormalizedDocumentIdentity | None]:
    """Compare exactly one prepared key freshly; never adopt an unexpected incumbent."""
    expected = next((key for key in preparation.keys if key.external_id == external_id), None)
    if expected is None:
        raise RuntimeError("normalized_preparation_key_missing")
    fresh, documents, identities = await _normalized_key_states(
        session, preparation.source_id, (external_id,), preparation.workspace_id,
    )
    if fresh != (expected,):
        raise RuntimeError("normalized_preparation_identity_changed")
    return expected, documents.get(external_id), identities.get(external_id)


class NormalizedDocumentValidationRejected(ValueError):
    """Reject one normalized record before any of that record's owner effects.

    Only explicit input/provider/order/collision validation emits this exception.
    Held identity/fence/SQL conflicts and all post-mutation failures remain transaction
    failures. ValueError compatibility preserves the ordinary acquiring API contract.
    """


async def upsert_normalized_document(
    session: AsyncSession, payload: NormalizedDocumentInput, *, scope: Scope, multi_workspace_enabled: bool,
) -> NormalizedDocumentResult:
    """Acquire Source then sorted Document/identity, retaining the ordinary result.

    Entry has no later domain/outbox locks. The owner captures current fences for
    this immediate operation and shares the held mutation body; caller commits.
    Deferred ingestion uses its original fences and explicit preparation instead.
    """
    source_fence = await sources.lock_source(
        session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source_fence is None or source_fence.status != "active"
            or source_fence.generation != payload.expected_source_generation):
        raise ValueError("Normalized source generation is no longer active")
    access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preparation = await prepare_normalized_document_keys(
        session, payload.source_id, (payload.provider_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    result, _ = await upsert_normalized_document_in_uow(
        session, payload, preparation=preparation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    return result


async def upsert_normalized_document_in_uow(
    session: AsyncSession, payload: NormalizedDocumentInput, *, preparation: NormalizedDocumentPreparation,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> tuple[NormalizedDocumentResult, NormalizedDocumentPreparation]:
    """Apply one prepared held key and return only its owner-produced successor.

    Actual admission/Source/Documents/identity locks remain held in this same
    transaction. Compare original full fences and exact current identity before
    writes; no FOR UPDATE, acquiring wrapper or ON CONFLICT winner adoption.
    Local inserted IDs are verified after flush and replace only this key. Unique
    failures and consistency conflicts abort the complete attempt; never commit.
    Explicit NormalizedDocumentValidationRejected guarantees this record has made no
    owner writes or successor changes. Other exceptions offer no such guarantee.
    """
    from modules.connectors import public as connectors

    try:
        payload = NormalizedDocumentInput.model_validate(payload.model_dump(mode="python"))
    except (ValueError, TypeError) as exc:
        raise NormalizedDocumentValidationRejected("Normalized input validation rejected") from exc
    if (not isinstance(preparation, NormalizedDocumentPreparation)
            or preparation.workspace_id != scope.workspace_id or preparation.source_id != payload.source_id
            or preparation.source_generation != payload.expected_source_generation):
        raise RuntimeError("normalized_preparation_header_changed")
    source_projection = await _normalized_source_proof(
        session, payload.source_id, payload.expected_source_generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    discriminator = payload.provenance.get("provider_scope_discriminator")
    if discriminator is not None or source_projection.provider in {"alpha_vantage", "open_meteo"}:
        provider_scope = await connectors.get_current_provider_scope(
            session, payload.source_id, payload.expected_source_generation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if (provider_scope is None or provider_scope.workspace_id != scope.workspace_id
                or provider_scope.source_id != payload.source_id
                or provider_scope.source_generation != payload.expected_source_generation
                or provider_scope.provider_id != source_projection.provider
                or provider_scope.discriminator != discriminator):
            raise RuntimeError("normalized_preparation_provider_changed")
    expected, document, identity = await _normalized_target(session, payload.provider_id, preparation)
    result, document, identity = await _apply_normalized_document(
        session, payload, source_projection, document, identity, preparation.workspace_id,
    )
    state = NormalizedDocumentKeyState(
        external_id=expected.external_id, document_id=document.id if document is not None else None,
        normalized_identity_id=identity.id if identity is not None else None,
        identity_document_id=identity.document_id if identity is not None else None,
        tombstoned_at=identity.tombstoned_at if identity is not None else None,
    )
    fresh, _, _ = await _normalized_key_states(session, payload.source_id, (payload.provider_id,), scope.workspace_id)
    if fresh != (state,):
        raise RuntimeError("normalized_preparation_local_write_changed")
    successor = NormalizedDocumentPreparation(
        workspace_id=preparation.workspace_id, source_id=preparation.source_id,
        source_generation=preparation.source_generation,
        keys=tuple(state if key.external_id == state.external_id else key for key in preparation.keys),
    )
    return result, successor


async def _apply_normalized_document(
    session: AsyncSession, payload: NormalizedDocumentInput, source_projection: ConnectorSource,
    document: Document | None, identity: NormalizedDocumentIdentity | None, workspace_id: UUID,
) -> tuple[NormalizedDocumentResult, Document | None, NormalizedDocumentIdentity | None]:
    """Share validation-first immutable revisions and provider ranking under held roots.

    Preserve tombstones, accepted-hash/version duplicates, generic and exact
    Telegram ordering, monotonic versions and World current selection ownership.
    Known malformed/colliding input emits NormalizedDocumentValidationRejected only
    before this record's first owner mutation. Unknown validation, consistency and
    all later database errors abort the transaction. Insert only proven absences.
    """
    provider_record = payload.provenance.get("provider_record")
    if provider_record is not None:
        try:
            typed_provider = ProviderRecordMetadata.model_validate(provider_record)
        except (ValueError, TypeError) as exc:
            raise NormalizedDocumentValidationRejected("Incoming provider provenance rejected") from exc
        if source_projection.provider != typed_provider.provider:
            raise NormalizedDocumentValidationRejected("Provider provenance does not match the immutable source provider")
    elif source_projection.provider == "telegram":
        raise NormalizedDocumentValidationRejected("Telegram normalization requires immutable delivery provenance")

    if identity is not None and identity.tombstoned_at is not None:
        return NormalizedDocumentResult(
            disposition="tombstoned", document_id=None, document_version_id=None,
            version_number=None, created_version=False, selected_current=False, chunk_count=0,
        ), document, identity
    if document is not None and (identity is None or identity.document_id is None):
        raise NormalizedDocumentValidationRejected("Provider identity conflicts with an existing non-normalized document")
    prior = await session.scalar(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.accepted_record_hash == payload.accepted_record_hash,
            NormalizedVersionProvenance.normalization_version == payload.normalization_version,
        )
    ) if document is not None else None
    if prior is not None:
        version = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.id == prior.document_version_id,
            DocumentVersion.document_id == document.id,
        ).execution_options(populate_existing=True))
        if version is None or prior.provider_id != payload.provider_id:
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
        ), document, identity

    current_provenance = None
    if document is not None and document.current_version:
        current_provenance = await session.scalar(
            select(NormalizedVersionProvenance)
            .join(DocumentVersion, DocumentVersion.id == NormalizedVersionProvenance.document_version_id)
            .where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version_number == document.current_version,
            )
        )
        if current_provenance is None:
            raise NormalizedDocumentValidationRejected("Provider identity conflicts with an owner-authored current revision")
    if source_projection.provider in {"alpha_vantage", "open_meteo"}:
        # Observation acceptance time and ingestion identity, not provider event
        # time or worker arrival order, own structured-series current selection.
        selected = False
    elif source_projection.provider == "telegram":
        try:
            incoming_metadata = ProviderRecordMetadata.model_validate(provider_record)
        except (ValueError, TypeError) as exc:
            raise NormalizedDocumentValidationRejected("Incoming Telegram provenance rejected") from exc
        incoming_telegram = incoming_metadata.telegram
        if incoming_telegram is None or payload.telegram_order is None:
            raise NormalizedDocumentValidationRejected("Telegram version ordering proof is missing")
        incoming_rank = (payload.observed_at, payload.telegram_order.epoch, payload.telegram_order.update_id)
        current_rank = None
        if current_provenance is not None:
            current_metadata = ProviderRecordMetadata.model_validate(
                current_provenance.provenance_json.get("provider_record")
            )
            current_telegram = current_metadata.telegram
            if current_telegram is None or current_telegram.bot_id != incoming_telegram.bot_id:
                raise NormalizedDocumentValidationRejected("Telegram current version has an incompatible bot binding")
            current_rank = (
                current_provenance.selection_observed_at,
                current_telegram.epoch,
                current_telegram.update_id,
            )
        if current_rank == incoming_rank and current_provenance is not None:
            if current_provenance.accepted_record_hash != payload.accepted_record_hash:
                raise NormalizedDocumentValidationRejected("Telegram delivery order has conflicting immutable content")
            raise NormalizedDocumentValidationRejected("Telegram delivery proof already exists with a different normalization version")
        selected = current_rank is None or incoming_rank > current_rank
    else:
        current_hash_rank = (
            (current_provenance.selection_observed_at, current_provenance.accepted_record_hash)
            if current_provenance is not None else None
        )
        selected = current_hash_rank is None or (payload.observed_at, payload.accepted_record_hash) > current_hash_rank
    # All provider/content errors above precede the first owner mutation.
    created_identity_id, created_document_id = None, None
    if identity is None:
        created_identity_id = uuid4()
        identity = NormalizedDocumentIdentity(id=created_identity_id, workspace_id=workspace_id,
                                              source_id=payload.source_id, external_id=payload.provider_id)
        session.add(identity)
    if document is None:
        created_document_id = uuid4()
        document = Document(
            id=created_document_id, workspace_id=workspace_id, source_id=payload.source_id, external_id=payload.provider_id,
            title=payload.title, content_type=payload.content_type,
            canonical_url=payload.canonical_url, published_at=payload.published_at,
            observed_at=payload.observed_at, current_version=0,
            content_hash=content_hash(payload.content), extraction_status="ready",
        )
        session.add(document)
        identity.document_id = document.id
        await session.flush()
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
    try:
        chunk_count = await add_content_chunks(session, version)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("normalized_preparation_chunk_write_conflict") from exc
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
    if (document.workspace_id != workspace_id or identity.workspace_id != workspace_id
            or document.source_id != payload.source_id or identity.source_id != payload.source_id
            or document.external_id != payload.provider_id or identity.external_id != payload.provider_id
            or identity.document_id != document.id or identity.tombstoned_at is not None
            or (created_document_id is not None and document.id != created_document_id)
            or (created_identity_id is not None and identity.id != created_identity_id)):
        raise RuntimeError("normalized_preparation_insert_changed")
    return NormalizedDocumentResult(
        disposition="normalized", document_id=document.id,
        document_version_id=version.id, version_number=version.version_number,
        created_version=True, selected_current=selected, chunk_count=chunk_count,
    ), document, identity


async def select_current_world_document_version(
    session: AsyncSession, *, document_id: UUID, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Acquire Source/Document/identity in order and preserve ordinary bool selection.

    Entry precedes later domain locks. Only an admitted exact workspace document
    can supply the Source/key. The held selector shares pointer mutation while
    deferred callers supply their own original fences and latest preparation.
    Missing source, document or exact selectable version returns False; no commit.
    """
    await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    locator = (await session.execute(select(Document.source_id, Document.external_id).where(
        Document.id == document_id, Document.workspace_id == scope.workspace_id,
    ))).one_or_none()
    if locator is None or locator.external_id is None:
        return False
    source_fence = await sources.lock_source(
        session, locator.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source_fence is None or source_fence.status != "active"
            or source_fence.generation != expected_source_generation):
        return False
    access_fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preparation = await prepare_normalized_document_keys(
        session, locator.source_id, (locator.external_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    return await select_current_world_document_version_in_uow(
        session, document_id=document_id, document_version_id=document_version_id,
        expected_source_generation=expected_source_generation,
        provider_scope_discriminator=provider_scope_discriminator, preparation=preparation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def normalized_observation_version_matches_in_uow(
    session: AsyncSession, *, source_id: UUID, source_generation: int,
    document_id: UUID, document_version_id: UUID, external_id: str,
    provider: str, provider_scope_discriminator: str,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Prove one exact held normalized World version through a nonlocking owner read.

    Original admission/Source and Document/identity parents remain held; this bool
    grants no write authority and exposes no ORM/content. Require same workspace,
    Source/key, live canonical identity, generation, typed provider and current
    non-secret provider scope. No current-version condition is imposed because O
    chooses current only after this proof. Accepted journal/SourceObservation
    lineage and FK parent preparation remain Ingestion's mandatory responsibility.
    Missing/deleted/mismatching evidence returns False, stale fences abort; no I/O.
    """
    from modules.connectors import public as connectors

    if (not isinstance(external_id, str) or not 1 <= len(external_id) <= 512
            or provider not in {"alpha_vantage", "open_meteo"}):
        return False
    projection = await _normalized_source_proof(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    current_scope = await connectors.get_current_provider_scope(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (projection.provider != provider or current_scope is None
            or current_scope.workspace_id != scope.workspace_id or current_scope.source_id != source_id
            or current_scope.source_generation != source_generation or current_scope.provider_id != provider
            or current_scope.discriminator != provider_scope_discriminator):
        return False
    rows = (await session.execute(select(NormalizedVersionProvenance.provenance_json).select_from(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(NormalizedDocumentIdentity, and_(
            NormalizedDocumentIdentity.document_id == Document.id,
            NormalizedDocumentIdentity.source_id == Document.source_id,
            NormalizedDocumentIdentity.external_id == Document.external_id,
            NormalizedDocumentIdentity.workspace_id == Document.workspace_id,
        ))
        .join(NormalizedVersionProvenance, and_(
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
            NormalizedVersionProvenance.document_id == Document.id,
        )).where(
            Document.id == document_id, Document.workspace_id == scope.workspace_id,
            Document.source_id == source_id, Document.external_id == external_id,
            DocumentVersion.id == document_version_id,
            NormalizedDocumentIdentity.tombstoned_at.is_(None),
            NormalizedVersionProvenance.provider_id == external_id,
            NormalizedVersionProvenance.source_generation == source_generation,
        ).limit(2))).all()
    if len(rows) != 1:
        return False
    provenance = rows[0][0]
    if not isinstance(provenance, dict) or provenance.get("provider_scope_discriminator") != provider_scope_discriminator:
        return False
    record = provenance.get("provider_record")
    if not isinstance(record, dict):
        return False
    try:
        metadata = ProviderRecordMetadata.model_validate(record)
    except (ValueError, TypeError):
        return False
    return metadata.provider == provider and metadata.world_data is not None


async def select_current_world_document_version_in_uow(
    session: AsyncSession, *, document_id: UUID, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
    preparation: NormalizedDocumentPreparation,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Select the O-approved exact World revision on an already-held prepared Document.

    Caller just selected this version through trusted O in this transaction.
    Compare original fences and latest key mapping; no Source/Document/version
    lock or acquiring helper is entered after outbox. Identity snapshot fields
    stay unchanged. Missing provenance returns False before mutation; no commit.
    """
    if (not isinstance(preparation, NormalizedDocumentPreparation) or preparation.workspace_id != scope.workspace_id
            or preparation.source_generation != expected_source_generation):
        raise RuntimeError("normalized_preparation_header_changed")
    projection = await _normalized_source_proof(
        session, preparation.source_id, expected_source_generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    matching = [key for key in preparation.keys if key.document_id == document_id and key.tombstoned_at is None]
    if len(matching) != 1:
        raise RuntimeError("normalized_preparation_document_missing")
    _, document, _ = await _normalized_target(session, matching[0].external_id, preparation)
    if document is None or not await normalized_observation_version_matches_in_uow(
        session, source_id=preparation.source_id, source_generation=expected_source_generation,
        document_id=document_id, document_version_id=document_version_id,
        external_id=matching[0].external_id, provider=projection.provider or "",
        provider_scope_discriminator=provider_scope_discriminator, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    ):
        return False
    return await _select_world_document_version(session, document, document_version_id,
                                                expected_source_generation, provider_scope_discriminator)


async def _select_world_document_version(
    session: AsyncSession, document: Document, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
) -> bool:
    """Mutate only an already-held Document's current/metadata projection, never lock.

    Exact version/provenance/source-key/generation/scope must match. The caller has
    freshly validated live identity/provider admission; missing evidence returns
    False before mutation. Projection flush is part of the caller's transaction.
    """
    selected = (await session.execute(
        select(DocumentVersion, NormalizedVersionProvenance)
        .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(
            DocumentVersion.id == document_version_id, DocumentVersion.document_id == document.id,
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.provider_id == document.external_id,
            NormalizedVersionProvenance.source_generation == expected_source_generation,
        ).execution_options(populate_existing=True)
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

async def _upload_document_state(
    session: AsyncSession, *, source_id: UUID, document_id: UUID, external_id: str,
    raw_uri: str, scope: Scope,
) -> tuple[tuple[UUID, ...], tuple[tuple[UUID, datetime | None], ...]]:
    """Read bounded UUID/key collision and identity state without acquiring earlier locks.

    Caller already proved and holds original Source/access. At most two canonical rows
    and one source-key identity can exist; foreign UUID collisions disclose no metadata
    outside this owner and are rejected. Source serialization protects absent key rows.
    """
    if (not isinstance(source_id, UUID) or not isinstance(document_id, UUID)
            or type(external_id) is not str or not 1 <= len(external_id) <= 512
            or type(raw_uri) is not str or not raw_uri):
        raise ValueError("Upload requires an exact Source, document, external key and raw URI")
    documents = tuple((await session.scalars(select(Document.id).where(or_(
        Document.id == document_id,
        and_(Document.source_id == source_id, Document.external_id == external_id),
    )).order_by(Document.id).limit(3))).all())
    identity_rows = (await session.execute(select(
        NormalizedDocumentIdentity.id, NormalizedDocumentIdentity.workspace_id, NormalizedDocumentIdentity.tombstoned_at,
    ).where(
        NormalizedDocumentIdentity.source_id == source_id,
        NormalizedDocumentIdentity.external_id == external_id,
    ).order_by(NormalizedDocumentIdentity.id).limit(2))).all()
    if len(documents) > 2 or len(identity_rows) > 1:
        raise RuntimeError("upload_document_identity_cardinality_changed")
    if any(row.workspace_id != scope.workspace_id for row in identity_rows):
        raise RuntimeError("upload_document_identity_namespace_changed")
    identities = tuple((row.id, row.tombstoned_at) for row in identity_rows)
    return documents, identities


async def _assert_upload_absence(
    session: AsyncSession, documents: tuple[UUID, ...], identities: tuple[tuple[UUID, datetime | None], ...], raw_uri: str,
) -> None:
    """Reject canonical/normalized reuse and globally captured raw identities without locks.

    Early preparation or late held insertion owns Source and exact URI lifecycle locks.
    The global receipt query is a conservative bool only: foreign IDs/content never leave
    Documents. Captured raw bytes cannot be republished, even after physical unlink.
    """
    if any(tombstoned_at is not None for _identity_id, tombstoned_at in identities):
        raise ValueError("Document identifier was previously deleted")
    if documents or identities:
        raise ValueError("Document identifier already exists")
    if await session.scalar(select(literal(True)).select_from(DocumentCleanupOperation).where(
        DocumentCleanupOperation.raw_uri == raw_uri,
    ).limit(1)):
        raise ValueError("This raw file identity was already deleted")


async def prepare_uploaded_document_in_uow(
    session: AsyncSession, *, source_id: UUID, document_id: UUID, external_id: str, raw_uri: str,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare a new upload after original Source/access, before any Ingestion root writes.

    Compare full original fences nonlockingly; lock discovered Documents in UUID order,
    then exact raw URI, then existing normalized identity. Reject any UUID/key collision,
    tombstone or prior raw cleanup capture before effects. Source/URI-held absence protects
    the later initializer; no token, registry, placeholder, mutation, event or commit exists.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("upload_original_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    await _normalized_source_proof(session, source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    documents, identities = await _upload_document_state(session, source_id=source_id,
        document_id=document_id, external_id=external_id, raw_uri=raw_uri, scope=scope)
    before = (documents, identities)
    if documents:
        await session.scalars(select(Document.id).where(Document.id.in_(documents)).order_by(Document.id).with_for_update())
    await lock_raw_uri_identity(session, raw_uri)
    if identities:
        await session.scalars(select(NormalizedDocumentIdentity.id).where(
            NormalizedDocumentIdentity.id.in_([identity_id for identity_id, _tombstoned_at in identities]),
        ).order_by(NormalizedDocumentIdentity.id).with_for_update())
    documents, identities = await _upload_document_state(session, source_id=source_id,
        document_id=document_id, external_id=external_id, raw_uri=raw_uri, scope=scope)
    if before != (documents, identities):
        raise RuntimeError("upload_document_preparation_changed")
    await _assert_upload_absence(session, documents, identities, raw_uri)


async def add_uploaded_document(
    session: AsyncSession,
    source_id: UUID,
    title: str,
    mime_type: str,
    raw_uri: str,
    metadata: dict[str, object],
    external_id: str,
    document_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> UUID:
    """Insert one held new upload root and empty queued version under exact original proof.

    Caller prepared exact Source/UUID/key/URI before Ingestion roots and retains those
    locks. Fresh nonlocking proof and no-republication checks do not acquire earlier
    Source/Document/URI/identity locks after I/outbox. Root inherits actual workspace;
    return UUID without commit/event/I/O. Caller owns atomic publication and raw rollback.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("upload_original_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    await _normalized_source_proof(session, source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    documents, identities = await _upload_document_state(session, source_id=source_id,
        document_id=document_id, external_id=external_id, raw_uri=raw_uri, scope=scope)
    await _assert_upload_absence(session, documents, identities, raw_uri)
    document = Document(
        id=document_id,
        workspace_id=scope.workspace_id,
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


async def _extraction_document(
    session: AsyncSession, document_id: UUID, source_id: UUID, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
    expected_raw_uri: str, expected_mime_type: str,
) -> Document | None:
    """Read one exact parser parent, comparing original URI/MIME and full fences.

    Early preparation uses this read before its singleton lock; late writes
    retain that Document/URI lock before Ingestion roots/outbox. No lock or authority fallback;
    missing/moved/deleted/raw-input mismatch returns None before mutation.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("extraction_original_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    await _normalized_source_proof(
        session, source_id, source_fence.generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if (type(expected_raw_uri) is not str or not expected_raw_uri
            or type(expected_mime_type) is not str or not 1 <= len(expected_mime_type) <= 255):
        raise RuntimeError("extraction_original_input_required")
    document = await session.scalar(select(Document).where(
        Document.id == document_id, Document.source_id == source_id, Document.workspace_id == scope.workspace_id,
        Document.raw_uri == expected_raw_uri, Document.mime_type == expected_mime_type,
    ).execution_options(populate_existing=True))
    if document is None or await session.scalar(select(DocumentCleanupOperation.id).where(
        DocumentCleanupOperation.raw_uri == expected_raw_uri,
    ).limit(1)) is not None:
        return None
    return document


async def lock_document_for_extraction(
    session: AsyncSession, document_id: UUID, source_id: UUID, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
    expected_raw_uri: str, expected_mime_type: str,
) -> bool:
    """Prepare singleton parser Document then URI lifecycle before any Ingestion roots.

    Caller retains original admitted workspace/Source locks. Freshly compare
    original complete fences, exact workspace/Source/Document/raw URI/MIME, lock
    the Document and then existing raw-URI lifecycle identity in deletion order.
    Missing/mismatching/deleted input returns False. No mutation, commit or I/O;
    Entities/Timeline/News callers require their own scoped owner conversion.
    """
    if await _extraction_document(
        session, document_id, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
        expected_raw_uri=expected_raw_uri, expected_mime_type=expected_mime_type,
    ) is None:
        return False
    document = await session.scalar(select(Document).where(
        Document.id == document_id, Document.source_id == source_id, Document.workspace_id == scope.workspace_id,
        Document.raw_uri == expected_raw_uri, Document.mime_type == expected_mime_type,
    ).with_for_update().execution_options(populate_existing=True))
    if document is None:
        return False
    await lock_raw_uri_identity(session, expected_raw_uri)
    return await _extraction_document(
        session, document_id, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
        expected_raw_uri=expected_raw_uri, expected_mime_type=expected_mime_type,
    ) is not None


async def set_extraction_status(
    session: AsyncSession, document_id: UUID, source_id: UUID, status: str, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
    expected_raw_uri: str, expected_mime_type: str,
) -> bool:
    """Flush parser status only on its already-held exact parent and original input.

    Retained early Document/URI locks and original admission/Source are mandatory;
    no earlier acquisition follows outbox. Invalid status is rejected before any
    mutation. Missing/deleted/mismatching input returns False, stale fences abort.
    """
    if status not in {"queued", "processing", "succeeded", "failed", "ready", "needs_ocr"}:
        raise ValueError("Invalid extraction status")
    document = await _extraction_document(
        session, document_id, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
        expected_raw_uri=expected_raw_uri, expected_mime_type=expected_mime_type,
    )
    if document is None:
        return False
    document.extraction_status = status
    await session.flush()
    return True

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
    *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
    expected_raw_uri: str, expected_mime_type: str,
) -> UUID | None:
    """Save parser output on its held parent, preserving original raw URI/MIME/fences.

    Prevalidate chunk conversion and metadata before mutation. Caller retains
    early admission/Source/Document/URI lifecycle locks; no earlier acquisition
    occurs after outbox. Immutable versions advance monotonically and chunks are
    added once; scoped ready events are new-row writes in the same transaction.
    Missing/mismatching input returns None before writes. Any later conversion,
    publication or consistency error aborts the complete attempt; no commit/I/O.
    """
    if type(text) is not str or extraction_status not in {"queued", "processing", "succeeded", "failed", "ready", "needs_ocr"}:
        raise ValueError("Invalid parser text or extraction status")
    prepared_chunks = [(str(chunk["content"]), int(cast("int", chunk["token_count"])),
                        dict(cast("dict[str, object]", chunk.get("metadata", {})))) for chunk in chunks]
    prepared_metadata, prepared_warnings = dict(extraction_metadata), list(warnings)
    document = await _extraction_document(
        session, document_id, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
        expected_raw_uri=expected_raw_uri, expected_mime_type=expected_mime_type,
    )
    if document is None:
        return None
    prepared_document_metadata = {
        **dict(document.metadata_json or {}), "extraction": prepared_metadata,
        "warnings": prepared_warnings, "parser": parser,
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
    document.metadata_json = prepared_document_metadata
    existing = await session.scalar(
        select(DocumentChunk.id).where(DocumentChunk.document_version_id == current.id).limit(1)
    )
    if existing is None:
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
    if extraction_status == "succeeded" and chunks:
        try:
            await _publish_extraction_ready(session, document, current, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("extraction_ready_publication_conflict") from exc
    await session.flush()
    return document.id


async def _publish_extraction_ready(
    session: AsyncSession, document: Document, version: DocumentVersion, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Publish the two Documents-owned parser ready events under original held proof.

    Use only exact local Document/version writes and captured Source generation.
    Ingestion public revalidates scope nonlockingly and inserts new outbox rows;
    no old claim is acquired, no Source ORM escapes and no commit or I/O occurs.
    """
    from modules.ingestion import public as ingestion

    await _normalized_source_proof(
        session, document.source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    payload = {"source_id": str(document.source_id), "document_id": str(document.id),
               "document_version_id": str(version.id), "source_generation": source_fence.generation,
               "version_number": version.version_number}
    for event_type in ("document.version.ready", "news.document.ready"):
        await ingestion.publish_event(session, DomainEvent(
            id=uuid4(), type=event_type, version=1, occurred_at=datetime.now(UTC),
            producer="modules.knowledge.documents", payload=payload,
        ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def _encode_document_owner_cursor(
    position: str, *, kind: str, fence: AccessFence, resource_id: UUID | None,
) -> str:
    """Wrap an existing keyset position in actor/workspace/epoch and exact selector context.

    This public wire value is neither a grant nor a signed snapshot. Source/root predicates
    remain mandatory on every page. No secret/key infrastructure or legacy fallback exists.
    """
    payload = [kind, str(fence.workspace_id), fence.user_id, fence.membership_revision,
               fence.configuration_revision, str(resource_id) if resource_id is not None else None, position]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_document_owner_cursor(
    cursor: str, *, kind: str, fence: AccessFence, resource_id: UUID | None,
) -> str:
    """Validate canonical bounded cursor context before returning the existing keyset position.

    Legacy/unscoped, malformed and other actor/workspace/filter/document cursors are 422;
    same-context stale membership/configuration is 409. Cursor edits confer no authority:
    real admission and Source/root predicates still precede each page query.
    """
    try:
        if type(cursor) is not str or not 1 <= len(cursor) <= 2048 or "=" in cursor:
            raise ValueError("Invalid cursor encoding")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Invalid cursor encoding")
        payload = json.loads(raw)
        if (not isinstance(payload, list) or len(payload) != 7 or type(payload[2]) is not int
                or type(payload[3]) is not int or payload[3] <= 0
                or type(payload[4]) is not int or payload[4] <= 0
                or type(payload[6]) is not str or not payload[6]):
            raise ValueError("Invalid cursor shape")
        if [payload[0], payload[1], payload[2], payload[5]] != [
            kind, str(fence.workspace_id), fence.user_id, str(resource_id) if resource_id is not None else None,
        ]:
            raise ValueError("Invalid cursor context")
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Invalid document cursor") from exc
    if payload[3:5] != [fence.membership_revision, fence.configuration_revision]:
        raise HTTPException(status_code=409, detail="Document page context changed")
    return str(payload[6])


async def list_documents(
    session: AsyncSession, limit: int, cursor: str | None, source_id: UUID | None, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> tuple[list[Document], str | None]:
    """Page <=100 retained owner roots, constraining workspace/Source before limit and cursor.

    Preserve created-time/UUID descending keyset semantics; optional foreign Source yields
    an empty page. Members cannot enumerate IDs/counts. Data-purged Sources are excluded,
    while paused/connector-only archived metadata remains readable. No locks or commit.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document list requires a workspace owner")
    fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Document page size must be between 1 and 100")
    statement = select(Document).where(*_document_scope(scope))
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    statement = statement.order_by(desc(Document.created_at), desc(Document.id))
    if cursor is not None:
        timestamp, identifier = decode_cursor(_decode_document_owner_cursor(
            cursor, kind="documents", fence=fence, resource_id=source_id))
        statement = statement.where(
            tuple_(Document.created_at, Document.id) < (timestamp, identifier)
        )
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_document_owner_cursor(encode_cursor(rows[-1].created_at, rows[-1].id),
        kind="documents", fence=fence, resource_id=source_id) if has_more and rows else None
    return rows, next_cursor


async def update_document(
    session: AsyncSession, document_id: UUID, payload: DocumentPatch, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document | None:
    """Patch an exact owner root freshly under Source→Document locks, then scoped replay.

    Never trust a previously read ORM. Retained inactive metadata is eligible except data
    purges. Missing/foreign root returns None; only actual title/metadata changes publish.
    Refresh generated fields before the commit releases original admission/domain locks.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document metadata writes require a workspace owner")
    source_id = await _read_document_source_id(session, document_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if source_id is None:
        return None
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    document = await session.scalar(select(Document).where(
        Document.id == document_id, Document.source_id == source_id, *_document_scope(scope),
    ).with_for_update().execution_options(populate_existing=True))
    if document is None:
        return None
    changed = False
    if "title" in payload.model_fields_set:
        value = payload.title or ""
        changed = changed or document.title != value
        document.title = value
    if "metadata" in payload.model_fields_set:
        metadata_value = payload.metadata or {}
        changed = changed or document.metadata_json != metadata_value
        document.metadata_json = metadata_value
    drafts = [make_knowledge_change(document.source_id, document.id, document.current_version, scope=scope)] if changed else []
    await session.flush()
    await session.refresh(document)
    await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=locked.access_fence)
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
    session: AsyncSession, document_id: UUID, expected_version: int, content: str, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document | None:
    """Append one owner revision under early active Source→Document locks and scoped replay.

    Missing/foreign/data-purged document or inactive Source returns None. Identical content
    is a no-op before expected-version CAS; changed stale content raises ValueError. Allocate
    max historical revision+1 while Source/key absence remains serialized, select it before
    scoped ready events, refresh under held locks and commit once. No external I/O.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document content writes require a workspace owner")
    source_id = await _read_document_source_id(session, document_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if source_id is None:
        return None
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    source_fence, access_fence = locked.fences[0], locked.access_fence
    if source_fence.status != "active":
        return None
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id, *_document_scope(scope))
        .with_for_update().execution_options(populate_existing=True)
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
    document.current_version = next_version
    document.content_hash = digest
    if await add_content_chunks(session, version):
        await _publish_document_ready(session, document, version, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await session.flush()
    await session.refresh(document)
    await commit_with_replay(
        session,
        [make_knowledge_change(document.source_id, document.id, next_version, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    return document


async def read_extraction_input(
    session: AsyncSession, version_id: UUID, allowed_chunk_ids: list[UUID] | None = None, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> ExtractionInput | None:
    """Return chunks for the active source's ready current version within extraction bounds.

    Raise ExtractionInputLimitError for empty, oversized or over-count input;
    unrelated selection validation remains ValueError. No partial input is returned.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ExtractionEvidenceRef] | None:
    """Validate a bounded set of current extraction chunks and return detached evidence refs."""
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not chunk_ids or len(chunk_ids) > 150 or len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Extraction membership evidence must be nonempty and bounded")
    rows = (await session.execute(
        select(Document.id, DocumentVersion.id, Document.source_id, Source.generation, DocumentChunk.id)
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
    )).all()
    if len(rows) != len(chunk_ids):
        return None
    return [ExtractionEvidenceRef(
        document_id=row[0], document_version_id=row[1], source_id=row[2],
        source_generation=row[3], chunk_id=row[4],
    ) for row in rows]


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
        local_only=local_only,
    ) for document_id, created_at, source_id, generation, version_id, version_number, local_only in rows]
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
    document_id, created_at, source_id, generation, actual_version_id, version_number, local_only = row
    return ReadyVersionRef(
        document_id=document_id, document_version_id=actual_version_id, source_id=source_id,
        source_generation=generation, version_number=version_number,
        created_at=created_at, local_only=local_only,
    )


async def _publish_document_ready(
    session: AsyncSession, document: Document, version: DocumentVersion, *,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Publish separate Documents ready events through the accepted held D0 publisher.

    Manual writers hold exact Source/Document and original access proof; current pointer
    already selects this immutable version. Ingestion owns the scoped eight-field event
    enrichment. No foreign Source ORM, parent lock reacquisition, commit or external I/O.
    The legacy backfill caller prepares the same mandatory proof per version.
    """
    await _publish_extraction_ready(session, document, version, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)


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
    session: AsyncSession, document_id: UUID, limit: int, cursor: str | None, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> tuple[list[DocumentVersion] | None, str | None]:
    """Page <=100 ascending immutable revisions through an admitted retained owner root.

    Workspace/Source deletion lineage is applied to parent and version query before LIMIT.
    Historical revisions remain exact, including inactive retained metadata; missing/foreign
    parent returns None. Members cannot enumerate versions. No lock, mutation or commit.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document history requires a workspace owner")
    fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not 1 <= limit <= 100:
        raise ValueError("Version page size must be between 1 and 100")
    after_version = decode_version_cursor(_decode_document_owner_cursor(
        cursor, kind="versions", fence=fence, resource_id=document_id)) if cursor is not None else None
    if await session.scalar(select(Document.id).where(Document.id == document_id, *_document_scope(scope))) is None:
        return None, None
    statement = select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id).where(
        Document.id == document_id, *_document_scope(scope),
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
    session: AsyncSession, document_id: UUID, number: int, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> DocumentVersion | None:
    """Read one exact historical owner revision through its retained scoped parent.

    Real owner admission precedes root/version predicates; inactive retained Source history
    remains visible except unfinished/failed data purges. No current-version substitution,
    member visibility, parent lock, mutation or commit; missing/foreign versions return None.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document history requires a workspace owner")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(
        select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == number,
            *_document_scope(scope),
        )
    )


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


async def _read_evidence_ref_rows(
    session: AsyncSession, refs: list[tuple[UUID, UUID]], *, scope: Scope,
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


async def _validate_gadget_document_selection_fences_admitted(
    session: AsyncSession, fences: tuple[GadgetDocumentSelectionFence, ...], *,
    scope: Scope, multi_workspace_enabled: bool, expected_access_fence: AccessFence,
    lock_rows: bool, max_documents: int,
) -> bool:
    """Validate an exact selection under its original admission fence without rereading access."""
    if (
        not fences or len(fences) > max_documents or max_documents > 100
        or len({item.document_id for item in fences}) != len(fences)
        or len({item.source_id for item in fences}) > 32
    ):
        raise ValueError("Selection fences exceed their bounded unique-document or source limit")
    source_ids = sorted({item.source_id for item in fences}, key=str)
    document_ids = sorted({item.document_id for item in fences}, key=str)
    if lock_rows:
        source_set = await sources.lock_source_set(
            session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=expected_access_fence,
        )
        if len(source_set.fences) != len(source_ids):
            return False
        locked_documents = (await session.scalars(
            select(Document).where(Document.id.in_(document_ids), *_document_scope(scope))
            .order_by(Document.id).with_for_update(read=True)
        )).all()
        if len(locked_documents) != len(document_ids):
            return False
    for fence in fences:
        components = await _current_document_components(
            session, fence.document_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            expected_source_generation=fence.source_generation, admitted=True,
        )
        if components is None:
            return False
        _document, version, source, provenance = components
        accepted_scope = provenance.provenance_json.get("provider_scope_discriminator") if provenance else None
        if (
            version.id != fence.document_version_id or source.id != fence.source_id
            or source.generation != fence.source_generation or source.type != fence.source_type
            or source.provider != fence.provider or source.local_only != fence.local_only
            or accepted_scope != fence.scope_discriminator
        ):
            return False
    return True


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
    scope: Scope, multi_workspace_enabled: bool,
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
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if selection_fences is not None and not await validate_gadget_document_selection_fences(
        session, selection_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")

    statement = (
        select(Document, DocumentVersion, DocumentChunk, Source)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs), *_document_scope(scope))
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
        ).execution_options(populate_existing=True)
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
    scope: Scope, multi_workspace_enabled: bool,
) -> list[ChatEvidenceChunk]:
    """Hold key-share locks on exact evidence through Chat's short publication transaction.

    Retained scoped identities are captured before locking in Source, Document, immutable version,
    then chunk order. A hard delete cannot commit between this current-evidence check and the
    caller's publication commit; callers must release locks promptly by committing or rolling back.
    """
    access_fence = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise ValueError("Evidence references must be unique and contain at most 100 items")
    if not refs:
        return []
    if selection_fences is not None and not await _validate_gadget_document_selection_fences_admitted(
        session, selection_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence, lock_rows=False, max_documents=32,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")
    ref_filter = tuple_(DocumentVersion.id, DocumentChunk.id).in_(refs)
    scoped_refs = list((await session.execute(
        select(Source.id, Document.id, DocumentVersion.id, DocumentChunk.id)
        .join(Document, Document.source_id == Source.id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .where(ref_filter, *_document_scope(scope))
        .order_by(Source.id, Document.id, DocumentVersion.id, DocumentChunk.id)
    )).all())
    source_ids = sorted({row[0] for row in scoped_refs}, key=str)
    document_ids = sorted({row[1] for row in scoped_refs}, key=str)
    version_ids = sorted({row[2] for row in scoped_refs}, key=str)
    chunk_ids = sorted({row[3] for row in scoped_refs}, key=str)
    if source_ids:
        await sources.lock_source_set(
            session, source_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
    if document_ids:
        await session.scalars(
        select(Document.id).where(Document.id.in_(document_ids), *_document_scope(scope)).order_by(Document.id)
            .with_for_update(read=True, key_share=True, of=Document)
        )
    if version_ids:
        await session.scalars(
            select(DocumentVersion.id).where(DocumentVersion.id.in_(version_ids)).order_by(DocumentVersion.id)
            .with_for_update(read=True, key_share=True, of=DocumentVersion)
        )
    if chunk_ids:
        await session.scalars(
            select(DocumentChunk.id).where(DocumentChunk.id.in_(chunk_ids)).order_by(DocumentChunk.id)
            .with_for_update(read=True, key_share=True, of=DocumentChunk)
        )
    if selection_fences is not None and not await _validate_gadget_document_selection_fences_admitted(
        session, selection_fences, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence, lock_rows=False, max_documents=32,
    ):
        raise ValueError("Selected document version or source privacy scope is stale")
    evidence = await read_chat_evidence_chunks(
        session, refs, require_active_source=require_active_source,
        require_current_version=require_current_version,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if selection_fences is not None and len(evidence) != len(refs):
        raise ValueError("One or more exact selected evidence chunks are unavailable")
    return evidence

