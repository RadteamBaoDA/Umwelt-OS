"""Memory and candidate export paging, provenance parsing and source-fence validation."""

import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, DomainError, PayloadTooLargeError, ValidationFailedError
from core.export_pages import content_digest, dto_digest
from core.pagination import b64url_decode, b64url_encode
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import Scope
from modules.memory._common import MEMORY_EXPORT_PAGE_MAX_BYTES, _admit
from modules.memory._privacy_cleanup import lock_export_privacy
from modules.memory.models import Memory, MemoryCandidate
from modules.memory.schemas import (
    MemoryCandidateExportRead,
    MemoryExportFence,
    MemoryExportFenceValidation,
    MemoryExportPage,
    MemoryExportProvenance,
    MemoryExportRead,
)

if TYPE_CHECKING:
    from modules.knowledge.documents.public import EvidenceReferenceRead


def _encode_memory_export_cursor(
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    created_at: datetime,
    identifier: UUID,
) -> str:
    """Bind keyset position to owner, record kind and fixed export cutoff."""
    value = json.dumps(
        [owner_id, record_kind, snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return b64url_encode(value)


def _decode_memory_export_cursor(
    cursor: str,
    owner_id: int,
    record_kind: str,
) -> tuple[datetime, datetime, UUID]:
    """Validate canonical owner-bound cursor fields before continuing a memory export."""
    try:
        if len(cursor) > 512 or "=" in cursor:
            raise ValueError
        raw = b64url_decode(cursor)
        if b64url_encode(raw) != cursor:
            raise ValueError
        values = json.loads(raw)
        if (
            not isinstance(values, list)
            or len(values) != 5
            or values[:2] != [owner_id, record_kind]
        ):
            raise ValueError
        snapshot_at, created_at = (
            datetime.fromisoformat(values[2]),
            datetime.fromisoformat(values[3]),
        )
        if any(
            item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, created_at)
        ):
            raise ValueError
        identifier = UUID(values[4])
        if (
            snapshot_at.isoformat() != values[2]
            or created_at.isoformat() != values[3]
            or created_at > snapshot_at
            or snapshot_at > datetime.now(UTC)
            or str(identifier) != values[4]
            or _encode_memory_export_cursor(
                owner_id, record_kind, snapshot_at, created_at, identifier
            )
            != cursor
        ):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError("Memory export cursor is invalid") from exc


def _memory_export_provenance(value: object) -> MemoryExportProvenance | None:
    """Project only documented provenance identifiers from arbitrary retained JSON."""
    if not isinstance(value, dict):
        return None
    allowed = {
        "conversation_id",
        "message_id",
        "source_id",
        "document_id",
        "document_version_id",
        "chunk_id",
        "origin",
    }
    return MemoryExportProvenance.model_validate(
        {key: item for key, item in value.items() if key in allowed}
    )


def _memory_export_read(
    item: Memory,
    provenance: MemoryExportProvenance | None = None,
) -> MemoryExportRead:
    """Project one retained memory record without its arbitrary JSON provenance keys."""
    return MemoryExportRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=provenance
        if provenance is not None
        else _memory_export_provenance(item.provenance),
        confidence=item.confidence,
        reason=item.reason,
        status=item.status,
        is_manual=item.is_manual,
        superseded_by_id=item.superseded_by_id,
        candidate_id=item.candidate_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
        invalidated_at=item.invalidated_at,
        forgotten_at=item.forgotten_at,
    )


def _candidate_export_read(
    item: MemoryCandidate,
    provenance: MemoryExportProvenance | None = None,
) -> MemoryCandidateExportRead:
    """Project one retained review candidate without arbitrary JSON provenance keys."""
    return MemoryCandidateExportRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=provenance
        if provenance is not None
        else _memory_export_provenance(item.provenance),
        confidence=item.confidence,
        novelty_score=item.novelty_score,
        usefulness_score=item.usefulness_score,
        reason=item.reason,
        status=item.status,
        rejection_reason=item.rejection_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
        evaluated_at=item.evaluated_at,
    )


_EXPORT_PROVENANCE_KEYS = {
    "conversation_id",
    "message_id",
    "source_id",
    "document_id",
    "document_version_id",
    "chunk_id",
    "origin",
}


@dataclass
class _ExportProvenance:
    """Parsed persisted provenance identifiers plus the evidence accumulated while verifying them."""

    raw: dict[str, Any]
    manual: bool
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    document_id: UUID | None = None
    version_id: UUID | None = None
    chunk_id: UUID | None = None
    explicit_source_id: UUID | None = None
    raw_doc_fields: bool = False
    complete_doc: bool = False
    safe: dict[str, object] = field(default_factory=dict)
    source_fence_data: dict[str, object] = field(default_factory=dict)
    chat_evidence: dict[str, object] = field(default_factory=dict)
    doc_evidence: "EvidenceReferenceRead | None" = None


def _parse_export_provenance(row: Memory | MemoryCandidate) -> _ExportProvenance:
    """Parse a row's provenance identifiers without trusting persisted JSON types.

    Non-manual rows with unknown keys or unparsable identifiers/origin raise ``ConflictError``
    (409): copied content must be fully verifiable. Pure; no I/O.
    """
    raw = row.provenance if isinstance(row.provenance, dict) else {}
    manual_memory = isinstance(row, Memory) and row.is_manual is True
    if raw.keys() - _EXPORT_PROVENANCE_KEYS and not manual_memory:
        raise ConflictError("Memory export cannot verify copied-content provenance")

    def optional_uuid(key: str) -> UUID | None:
        """Parse one optional provenance identifier without trusting persisted JSON types."""
        value = raw.get(key)
        if value is None:
            return None
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    parsed = _ExportProvenance(
        raw=raw,
        manual=manual_memory,
        conversation_id=optional_uuid("conversation_id"),
        message_id=optional_uuid("message_id"),
        document_id=optional_uuid("document_id"),
        version_id=optional_uuid("document_version_id"),
        chunk_id=optional_uuid("chunk_id"),
        explicit_source_id=optional_uuid("source_id"),
    )
    parsed.raw_doc_fields = any(
        raw.get(key) is not None for key in ("document_id", "document_version_id", "chunk_id")
    )
    parsed.complete_doc = all(
        value is not None for value in (parsed.document_id, parsed.version_id, parsed.chunk_id)
    )
    if not manual_memory:
        if (
            (raw.get("conversation_id") is not None and parsed.conversation_id is None)
            or (raw.get("message_id") is not None and parsed.message_id is None)
            or (raw.get("source_id") is not None and parsed.explicit_source_id is None)
            or (
                raw.get("origin") is not None
                and (
                    not isinstance(raw.get("origin"), str)
                    or raw.get("origin") not in {"agent", "model"}
                )
            )
        ):
            raise ConflictError("Memory export cannot verify copied-content provenance")
    return parsed


async def _verify_export_chat_evidence(
    session: AsyncSession,
    prov: _ExportProvenance,
    *,
    scope: Scope,
) -> None:
    """Verify retained Chat message evidence; fills ``prov.safe``/``prov.chat_evidence``.

    A partial or removed transcript reference is a 409 unless the Memory is manual, which
    simply drops the annotation. Resolved under the caller's workspace owner. Read-only.
    """
    from modules.chat import public as chat_public

    if prov.conversation_id is None and prov.message_id is None:
        return
    if prov.conversation_id is None or prov.message_id is None:
        if not prov.manual:
            raise ConflictError("Memory export cannot verify partial conversation provenance")
        return
    origin = await chat_public.read_memory_export_origin(
        session,
        owner_id=_actor(scope),
        conversation_id=prov.conversation_id,
        message_id=prov.message_id,
    )
    if origin is None:
        if not prov.manual:
            raise ConflictError("Memory transcript evidence was removed or is not retained")
        return
    prov.safe.update(conversation_id=prov.conversation_id, message_id=prov.message_id)
    prov.chat_evidence = {
        "conversation_id": origin.conversation_id,
        "message_id": origin.message_id,
        "chat_evidence_digest": hashlib.sha256(
            origin.model_dump_json().encode("utf-8")
        ).hexdigest(),
        "chat_privacy_persisted": origin.privacy_persisted,
        "chat_privacy_updated_at": origin.privacy_updated_at,
    }


async def _verify_export_document_evidence(
    session: AsyncSession,
    prov: _ExportProvenance,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Verify the exact retained Document chunk evidence; fills ``prov.doc_evidence``/``prov.safe``.

    Incomplete or removed Document evidence is a 409 unless the Memory is manual. A mismatching
    document or explicit source also counts as removed. Read-only.
    """
    from modules.knowledge.documents import public as documents_public

    if not prov.raw_doc_fields:
        return
    if not prov.complete_doc:
        if not prov.manual:
            raise ConflictError("Memory export cannot verify incomplete document provenance")
        return
    try:
        # complete_doc
        if prov.version_id is None or prov.chunk_id is None:
            raise RuntimeError(
                "invariant violated: prov.version_id and prov.chunk_id must not be None"
            )
        refs = await documents_public.read_evidence_refs(
            session,
            [(prov.version_id, prov.chunk_id)],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except ValueError:
        refs = []
    if (
        len(refs) == 1
        and refs[0].document_id == prov.document_id
        and (prov.explicit_source_id is None or refs[0].source_id == prov.explicit_source_id)
    ):
        prov.doc_evidence = refs[0]
        prov.safe.update(
            document_id=prov.document_id,
            document_version_id=prov.version_id,
            chunk_id=prov.chunk_id,
        )
    elif not prov.manual:
        raise ConflictError("Memory document evidence was removed or is being purged")


async def _fence_export_source(
    session: AsyncSession,
    prov: _ExportProvenance,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Fence the owning Source of the verified evidence; fills ``prov.source_fence_data``.

    Export eligibility reopens when a purge succeeds, so source-only copies also need the
    durable fence: once any data purge exists, no new Source-tied copy may be admitted. An
    ineligible Source is a 409 for derived copies and strips the Source/Document annotations of
    manual ones. Read-only.
    """
    from modules.sources import public as sources_public
    from modules.sources.public import SourceExportFence

    source_id = prov.explicit_source_id
    if prov.doc_evidence is not None:
        source_id = prov.doc_evidence.source_id
    if source_id is None:
        return
    source = await sources_public.get_source_fence(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    eligible = bool(
        source
        and await sources_public.filter_export_eligible_sources(
            session,
            [
                SourceExportFence(
                    source_id=source_id,
                    workspace_id=scope.workspace_id,
                    generation=source.generation,
                )
            ],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        and not await sources_public.source_data_purge_exists(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    )
    if not eligible:
        if not prov.manual:
            raise ConflictError("Memory source evidence was removed or is being purged")
        prov.safe.pop("source_id", None)
        if prov.doc_evidence is not None:
            for key in ("document_id", "document_version_id", "chunk_id"):
                prov.safe.pop(key, None)
            prov.doc_evidence = None
    else:
        prov.safe["source_id"] = source_id
        if source is None:
            raise RuntimeError("invariant violated: source must not be None")
        prov.source_fence_data = {"source_id": source.id, "source_generation": source.generation}


async def _memory_export_source_fence(
    session: AsyncSession,
    row: Memory | MemoryCandidate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[dict[str, object], MemoryExportProvenance | None]:
    """Project only independently verified provenance and fence its live owner evidence.

    Chat, Document and Source evidence is resolved under the caller's workspace scope, so a
    copy whose provenance names a foreign-workspace identity is treated as removed.

    Explicitly manual Memory remains exportable after optional evidence is removed. Every
    model-derived or candidate copy needs exact retained Chat message or document evidence.
    Read-only; verification order is parse, Chat, Document, then Source.
    """
    prov = _parse_export_provenance(row)
    await _verify_export_chat_evidence(session, prov, scope=scope)
    await _verify_export_document_evidence(
        session,
        prov,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _fence_export_source(
        session, prov, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if prov.doc_evidence is not None and prov.source_fence_data:
        prov.safe.update(
            document_id=prov.document_id,
            document_version_id=prov.version_id,
            chunk_id=prov.chunk_id,
        )
        prov.source_fence_data.update(
            document_id=prov.document_id,
            document_version_id=prov.version_id,
            chunk_id=prov.chunk_id,
        )
    elif prov.doc_evidence is not None:
        prov.source_fence_data.update(
            document_id=prov.document_id,
            document_version_id=prov.version_id,
            chunk_id=prov.chunk_id,
        )

    if not prov.doc_evidence and not prov.chat_evidence and not prov.manual:
        raise ConflictError("Memory export cannot verify copied-content provenance")
    if prov.manual:
        prov.safe["origin"] = "manual"
    elif isinstance(prov.raw.get("origin"), str) and prov.raw.get("origin") in {"agent", "model"}:
        prov.safe["origin"] = prov.raw["origin"]
    projected = MemoryExportProvenance.model_validate(prov.safe) if prov.safe else None
    return {**prov.source_fence_data, **prov.chat_evidence}, projected


def _memory_export_scope(
    record_kind: str,
    snapshot_at: datetime,
    scope: Scope,
) -> tuple[ColumnElement[bool], ...]:
    """Select only this workspace's records created and last changed by the immutable page cutoff."""
    if record_kind == "memories":
        return (
            Memory.workspace_id == scope.workspace_id,
            Memory.status != "forgotten",
            Memory.created_at <= snapshot_at,
            Memory.updated_at <= snapshot_at,
        )
    return (
        MemoryCandidate.workspace_id == scope.workspace_id,
        MemoryCandidate.created_at <= snapshot_at,
        MemoryCandidate.updated_at <= snapshot_at,
    )


async def _memory_export_count(
    session: AsyncSession,
    record_kind: str,
    snapshot_at: datetime,
    scope: Scope,
) -> int:
    """Count the retained workspace inventory at one fixed cutoff for page and final checks."""
    model: type[Memory | MemoryCandidate] = Memory if record_kind == "memories" else MemoryCandidate
    return int(
        await session.scalar(
            select(func.count())
            .select_from(model)
            .where(*_memory_export_scope(record_kind, snapshot_at, scope))
        )
        or 0
    )


async def _memory_export_rows(
    session: AsyncSession,
    record_kind: str,
    snapshot_at: datetime,
    position: tuple[datetime, UUID] | None,
    fetch: int,
    *,
    scope: Scope,
) -> list[Memory | MemoryCandidate]:
    """Fetch up to ``fetch`` export rows in stable ``(created_at, id)`` order after ``position``.

    The workspace/cutoff predicate comes from ``_memory_export_scope`` and precedes ordering and
    LIMIT; callers pass ``limit + 1`` to detect a following page. Rows are refreshed with
    ``populate_existing`` so stale identity-map state never reaches the export.
    """
    model: type[Memory | MemoryCandidate] = Memory if record_kind == "memories" else MemoryCandidate
    statement = select(model).where(*_memory_export_scope(record_kind, snapshot_at, scope))
    if position is not None:
        statement = statement.where(tuple_(model.created_at, model.id) > position)
    # `model` is chosen by record_kind, so every row is exactly one of the two ORM types.
    return cast(
        list[Memory | MemoryCandidate],
        list(
            (
                await session.scalars(
                    statement.order_by(model.created_at, model.id)
                    .limit(fetch)
                    .execution_options(populate_existing=True)
                )
            ).all()
        ),
    )


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    scope: Scope,
    multi_workspace_enabled: bool,
    limit: int = 50,
    cursor: str | None = None,
) -> MemoryExportPage:
    """Return a bounded workspace page after admission, the privacy fence and evidence validation.

    ``owner_id`` must equal the scope actor. The immutable cutoff and content digests support the
    export publisher's second source/owner fence. No arbitrary copied provenance JSON is included
    in the portable projection. The workspace predicate precedes ordering and LIMIT.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    if (
        owner_id != _actor(scope)
        or record_kind not in {"memories", "candidates"}
        or not 1 <= limit <= 100
    ):
        raise ValueError("Memory export owner, kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_memory_export_cursor(
            cursor, owner_id, record_kind
        )
        position = (position_at, position_id)
    rows = await _memory_export_rows(
        session, record_kind, snapshot_at, position, limit + 1, scope=scope
    )
    has_more, rows = len(rows) > limit, rows[:limit]
    items: list[MemoryExportRead | MemoryCandidateExportRead] = []
    fences: list[MemoryExportFence] = []
    omitted_count = 0
    payload_bytes = 2
    for row in rows:
        try:
            provenance_fence, provenance = await _memory_export_source_fence(
                session,
                row,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except (HTTPException, DomainError) as exc:
            if exc.status_code != 409:
                raise
            omitted_count += 1
            continue
        item = (
            _memory_export_read(cast(Memory, row), provenance)
            if record_kind == "memories"
            else _candidate_export_read(cast(MemoryCandidate, row), provenance)
        )
        raw = item.model_dump_json().encode("utf-8")
        proposed_bytes = payload_bytes + len(raw) + (1 if items else 0)
        if proposed_bytes > MEMORY_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("Memory export record exceeds its page byte bound")
            raise PayloadTooLargeError("Memory export page exceeds its byte bound")
        payload_bytes = proposed_bytes
        items.append(item)
        fences.append(
            MemoryExportFence(
                record_kind="memory" if record_kind == "memories" else "candidate",
                id=row.id,
                created_at=row.created_at,
                updated_at=row.updated_at,
                content_digest=content_digest(raw),
                **provenance_fence,
            )
        )
    return MemoryExportPage(
        owner_id=owner_id,
        record_kind=record_kind,
        snapshot_at=snapshot_at,
        snapshot_count=await _memory_export_count(session, record_kind, snapshot_at, scope),
        omitted_count=omitted_count,
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        max_payload_bytes=MEMORY_EXPORT_PAGE_MAX_BYTES,
        available=omitted_count == 0,
        omission_reason="unsupported_provenance" if omitted_count else None,
        next_cursor=_encode_memory_export_cursor(
            owner_id, record_kind, snapshot_at, rows[-1].created_at, rows[-1].id
        )
        if has_more and rows
        else None,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[MemoryExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> MemoryExportFenceValidation:
    """Recheck admission, privacy, retained workspace rows, exact content and inventory before publication."""
    try:
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except (HTTPException, DomainError) as exc:
        if exc.status_code not in {401, 404}:
            raise
        return MemoryExportFenceValidation(
            valid=False, reason="owner_unavailable", observed_snapshot_count=0
        )
    await lock_export_privacy(session, scope.workspace_id, shared=True)
    if (
        owner_id != _actor(scope)
        or record_kind not in {"memories", "candidates"}
        or len(fences) > 100
    ):
        raise ValueError("Memory export validation input is invalid")
    observed = await _memory_export_count(session, record_kind, snapshot_at, scope)
    if observed != expected_snapshot_count:
        return MemoryExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    model: type[Memory | MemoryCandidate] = Memory if record_kind == "memories" else MemoryCandidate
    for fence in fences:
        row: Memory | MemoryCandidate | None = await session.scalar(
            select(model)
            .where(
                model.id == fence.id,
                *_memory_export_scope(record_kind, snapshot_at, scope),
            )
            .execution_options(populate_existing=True)
        )
        if row is None or row.created_at != fence.created_at or row.updated_at != fence.updated_at:
            return MemoryExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        try:
            source_fence, provenance = await _memory_export_source_fence(
                session,
                row,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except (HTTPException, DomainError):
            return MemoryExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        item = (
            _memory_export_read(cast(Memory, row), provenance)
            if record_kind == "memories"
            else _candidate_export_read(cast(MemoryCandidate, row), provenance)
        )
        digest = dto_digest(item)
        if digest != fence.content_digest:
            return MemoryExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        expected_source_fence = fence.model_dump(
            exclude={"record_kind", "id", "created_at", "updated_at", "content_digest"},
            exclude_none=True,
        )
        if source_fence != expected_source_fence:
            return MemoryExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return MemoryExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)
