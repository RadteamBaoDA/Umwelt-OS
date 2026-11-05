"""Owner data export: fenced, byte-bounded pages of document headers and revisions."""

import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    and_,
    func,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode
from core.workspaces.schemas import (
    AccessFence,
    InternalJobScope,
    Scope,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentInteraction,
    DocumentVersion,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    DocumentExportFence,
    DocumentExportFenceValidation,
    DocumentExportPage,
    DocumentExportProvenance,
    DocumentExportRead,
    DocumentVersionExportRead,
    ObservationExportEvidenceCandidate,
    ObservationExportEvidenceRead,
    TimelineExportEvidenceCandidate,
    TimelineExportEvidenceRead,
)
from modules.sources import public as sources
from modules.sources.models import Source
from modules.sources.schemas import (
    SourceExportFence,
)

if TYPE_CHECKING:
    from modules.connectors.public import ProviderScopeSnapshot

EXPORT_PAGE_MAX_BYTES = 16_777_216


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
    return b64url_encode(raw)


def _decode_document_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID, record_kind: str, access_fence: AccessFence,
) -> tuple[datetime, datetime, UUID]:
    """Decode a canonical cursor and reject owner, filter, or admission-revision changes."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError("Invalid document export cursor")
        raw = b64url_decode(cursor)
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


@dataclass
class _ExportPageBuilder:
    """Accumulate export records under the row limit and the exact JSON byte budget.

    Mirrors the page contract: records are admitted in order, ``payload_bytes`` starts at the two
    bytes of the JSON array brackets and grows by each record plus a separating comma. A record
    that would exceed the budget ends the page (``has_more``) unless it is the first record, in
    which case it can never be exported and ``ValueError`` is raised.
    """

    limit: int
    overflow_message: str
    items: list[DocumentExportRead | DocumentVersionExportRead] = field(default_factory=list)
    fences: list[DocumentExportFence] = field(default_factory=list)
    has_more: bool = False
    payload_bytes: int = 2

    def full(self) -> bool:
        """Return whether the row limit is reached; marks the page as having a continuation."""
        if len(self.items) == self.limit:
            self.has_more = True
            return True
        return False

    def add(
        self, item: DocumentExportRead | DocumentVersionExportRead,
        make_fence: Callable[[], DocumentExportFence],
    ) -> bool:
        """Append one record and its fence, or return ``False`` when the byte budget ends the page.

        The fence is built only after the byte-budget check, so an over-budget record fails (or
        ends the page) before any fence validation, exactly like the pre-split implementation.

        Raises:
            ValueError: the very first record alone exceeds the page byte budget.
        """
        proposed_bytes = self.payload_bytes + _export_item_bytes(item) + (1 if self.items else 0)
        if proposed_bytes > EXPORT_PAGE_MAX_BYTES:
            if not self.items:
                raise ValueError(self.overflow_message)
            self.has_more = True
            return False
        self.items.append(item)
        self.payload_bytes = proposed_bytes
        self.fences.append(make_fence())
        return True


def _document_header_statement(
    scope: Scope, snapshot_at: datetime, position: tuple[datetime, UUID] | None, limit: int,
) -> Any:
    """Build the keyset-ordered document-header export query (``limit + 1`` rows, no execution)."""
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
    return statement.order_by(Document.created_at, Document.id).limit(limit + 1)


def _document_header_item(row: Any) -> DocumentExportRead:
    """Project one document-header row into its safe export item.

    Raises:
        ValueError: the retained document has no current immutable version.
    """
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
    return item


def _document_header_fence(row: Any) -> DocumentExportFence:
    """Build the revalidation fence (identity, timestamps, source generation) of a header row."""
    return DocumentExportFence(
        document_id=row["document_id"], document_created_at=row["document_created_at"],
        document_updated_at=row["document_updated_at"], document_current_version=row["document_current_version"],
        source_id=row["source_id"], source_status=row["source_status"],
        current_source_generation=row["source_generation"],
    )


def _version_export_statement(
    owner_id: int, scope: Scope, snapshot_at: datetime, position: tuple[datetime, UUID] | None, limit: int,
) -> Any:
    """Build the keyset-ordered full-revision export query (``limit + 1`` rows, no execution)."""
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
            # Interaction state is read live (best-effort), not covered by snapshot_at/fences: a
            # read/save/hide between export pages can yield a mixed view. Accepted by design.
            DocumentInteraction.read_at.label("interaction_read_at"),
            DocumentInteraction.bookmarked_at.label("interaction_bookmarked_at"),
            DocumentInteraction.dismissed_at.label("interaction_dismissed_at"),
        )
        .join(Source, Source.id == Document.source_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .outerjoin(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .outerjoin(DocumentInteraction, and_(
            DocumentInteraction.document_version_id == DocumentVersion.id,
            DocumentInteraction.owner_id == owner_id,
        ))
        .where(
            Document.workspace_id == scope.workspace_id,
            *_document_export_scope(snapshot_at), DocumentVersion.created_at <= snapshot_at,
            Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
        )
    )
    if position is not None:
        version_statement = version_statement.where(tuple_(DocumentVersion.created_at, DocumentVersion.id) > position)
    return (
        version_statement.order_by(DocumentVersion.created_at, DocumentVersion.id)
        .limit(limit + 1).execution_options(yield_per=10)
    )


def _version_export_item(version_row: Any) -> DocumentVersionExportRead:
    """Project one revision row into its safe export item (provenance only when normalized)."""
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
        read_at=version_row["interaction_read_at"],
        bookmarked_at=version_row["interaction_bookmarked_at"],
        dismissed_at=version_row["interaction_dismissed_at"],
    )
    return version_item


def _version_export_fence(version_row: Any) -> DocumentExportFence:
    """Build the revalidation fence (document, source generation, revision digest) of a row."""
    return DocumentExportFence(
        document_id=version_row["document_id"], document_created_at=version_row["document_created_at"],
        document_updated_at=version_row["document_updated_at"], document_current_version=version_row["document_current_version"],
        source_id=version_row["source_id"], source_status=version_row["source_status"],
        current_source_generation=version_row["source_generation"], version_id=version_row["version_id"],
        version_number=version_row["version_number"], version_created_at=version_row["version_created_at"],
        version_content_digest=version_row["version_content_hash"],
    )


async def _collect_document_headers(
    session: AsyncSession, builder: _ExportPageBuilder, statement: Any,
) -> None:
    """Fill ``builder`` from the header query, stopping at the row limit or byte budget.

    ``has_more`` is also set when the query returned more rows than were admitted.
    """
    rows = (await session.execute(statement)).all()
    for raw_row in rows:
        if builder.full():
            break
        row = raw_row._mapping
        if not builder.add(_document_header_item(row), partial(_document_header_fence, row)):
            break
    if len(rows) > len(builder.items):
        builder.has_more = True


async def _collect_versions(
    session: AsyncSession, builder: _ExportPageBuilder, statement: Any,
) -> None:
    """Stream revision rows into ``builder`` (yield_per batches), always closing the stream."""
    result = await session.stream(statement)
    try:
        async for version_raw_row in result.mappings():
            if builder.full():
                break
            if not builder.add(
                _version_export_item(version_raw_row),
                partial(_version_export_fence, version_raw_row),
            ):
                break
    finally:
        await result.close()


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

    if record_kind == "documents":
        builder = _ExportPageBuilder(limit, "A document export record exceeds the page byte budget")
        await _collect_document_headers(
            session, builder, _document_header_statement(scope, snapshot_at, position, limit))
    else:
        builder = _ExportPageBuilder(limit, "A document version exceeds the page byte budget")
        await _collect_versions(
            session, builder, _version_export_statement(owner_id, scope, snapshot_at, position, limit))
    items, fences = builder.items, builder.fences
    next_cursor = (
        _encode_document_export_cursor(
            owner_id, scope.workspace_id, record_kind, snapshot_at, items[-1].created_at, items[-1].id,
            access_fence,
        )
        if builder.has_more and items else None
    )

    if len(items) != len(fences):
        raise RuntimeError("Document export page lost a record fence")
    return DocumentExportPage(
        owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences,
        payload_bytes=_export_payload_bytes(items), max_payload_bytes=EXPORT_PAGE_MAX_BYTES,
        next_cursor=next_cursor, available=True, omission_reason=None,
    )


async def _recheck_export_fence(
    session: AsyncSession, fence: DocumentExportFence, *, record_kind: str, scope: Scope,
    observed_count: int,
) -> DocumentExportFenceValidation | None:
    """Recheck one record fence against the live rows; return the invalidation or ``None`` if valid.

    Reads the document/source row (and, for ``versions``, the immutable revision) without
    locking. Order is part of the contract: record change, then source drift, then revision
    identity. Raises ``ValueError`` for structurally impossible fences (a version fence without
    its revision identity, or a header fence that carries one).
    """
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
    return None


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
        invalid = await _recheck_export_fence(
            session, fence, record_kind=record_kind, scope=scope, observed_count=observed_count)
        if invalid is not None:
            return invalid
    return DocumentExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed_count)


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
    *, scope: Scope, multi_workspace_enabled: bool, include_historical: bool = False,
) -> dict[UUID, int]:
    """Return observation evidence revisions backed by matching document and provider fences.

    ``include_historical`` (observation history mode only) also accepts superseded document versions of the
    same document: every other fence (scope, source generation, provider identity, provenance) still applies.

    Documents owns version/provenance reads. The result contains accepted observation IDs
    paired with the version number of the document version that backs each accepted observation
    (the current version unless ``include_historical``); foreign modules never receive Document ORM
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
            and (include_historical or version_number == current_version) and provider_id == candidate.external_id
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
    for final fencing; paused or archived lifecycle changes do not rewrite historical acceptance.
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
