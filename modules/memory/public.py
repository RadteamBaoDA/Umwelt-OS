"""Public contract and service interface for selective memory, candidates, and privacy management."""

from datetime import UTC, datetime
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from core.auth.models import Owner
from redis.asyncio import Redis
from sqlalchemy import delete, desc, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.memory.schemas import (
    MemoryCandidatePage,
    MemoryCandidateRead,
    MemoryCreate,
    MemoryExportPrivacy,
    MemoryExportFence,
    MemoryExportFenceValidation,
    MemoryExportPage,
    MemoryExportProvenance,
    MemoryExportRead,
    MemoryCandidateExportRead,
    MemoryPage,
    MemoryPrivacyConfig,
    MemoryPrivacyUpdate,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRead,
    MemorySupersedeRequest,
    MemoryUpdate,
)
from modules.memory.selection import (
    evaluate_candidate,
    extract_candidate_proposals,
)

logger = logging.getLogger(__name__)

CACHE_KEY_MEMORIES_ACTIVE = "cache:memory:active"
MEMORY_EXPORT_PAGE_MAX_BYTES = 16_777_216
_DOCUMENT_MEMORY_CLEANUP_REASON = "Source document evidence removed"
_DOCUMENT_MEMORY_UNRESOLVED_REASON = "legacy_provenance_unresolved"


@dataclass(frozen=True)
class DocumentMemoryCleanupProgress:
    """Describe one bounded Memory cleanup page without exposing copied content."""

    complete: bool
    next_cursor: str | None
    changed: bool
    scrubbed_memories: int
    scrubbed_candidates: int
    unresolved_count: int
    unresolved_reason: str | None


def _encode_memory_export_cursor(
    owner_id: int, record_kind: str, snapshot_at: datetime, created_at: datetime, identifier: UUID,
) -> str:
    """Bind keyset position to owner, record kind and fixed export cutoff."""
    value = json.dumps(
        [owner_id, record_kind, snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_memory_export_cursor(
    cursor: str, owner_id: int, record_kind: str,
) -> tuple[datetime, datetime, UUID]:
    """Validate canonical owner-bound cursor fields before continuing a memory export."""
    try:
        if len(cursor) > 512 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 5 or values[:2] != [owner_id, record_kind]:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(values[2]), datetime.fromisoformat(values[3])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot_at, created_at)):
            raise ValueError
        identifier = UUID(values[4])
        if (snapshot_at.isoformat() != values[2] or created_at.isoformat() != values[3]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != values[4]
                or _encode_memory_export_cursor(owner_id, record_kind, snapshot_at, created_at, identifier) != cursor):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Memory export cursor is invalid") from exc


def _memory_export_provenance(value: object) -> MemoryExportProvenance | None:
    """Project only documented provenance identifiers from arbitrary retained JSON."""
    if not isinstance(value, dict):
        return None
    allowed = {"conversation_id", "message_id", "source_id", "document_id", "document_version_id", "chunk_id", "origin"}
    return MemoryExportProvenance.model_validate({key: item for key, item in value.items() if key in allowed})


def _memory_export_read(
    item: Memory, provenance: MemoryExportProvenance | None = None,
) -> MemoryExportRead:
    """Project one retained memory record without its arbitrary JSON provenance keys."""
    return MemoryExportRead(
        id=item.id, content=item.content, type=item.memory_type,
        provenance=provenance if provenance is not None else _memory_export_provenance(item.provenance),
        confidence=item.confidence,
        reason=item.reason, status=item.status, is_manual=item.is_manual,
        superseded_by_id=item.superseded_by_id, candidate_id=item.candidate_id,
        created_at=item.created_at, updated_at=item.updated_at,
        invalidated_at=item.invalidated_at, forgotten_at=item.forgotten_at,
    )


def _candidate_export_read(
    item: MemoryCandidate, provenance: MemoryExportProvenance | None = None,
) -> MemoryCandidateExportRead:
    """Project one retained review candidate without arbitrary JSON provenance keys."""
    return MemoryCandidateExportRead(
        id=item.id, content=item.content, type=item.memory_type,
        provenance=provenance if provenance is not None else _memory_export_provenance(item.provenance),
        confidence=item.confidence,
        novelty_score=item.novelty_score, usefulness_score=item.usefulness_score,
        reason=item.reason, status=item.status, rejection_reason=item.rejection_reason,
        created_at=item.created_at, updated_at=item.updated_at, evaluated_at=item.evaluated_at,
    )


async def _memory_export_source_fence(
    session: AsyncSession, row: Memory | MemoryCandidate,
) -> tuple[dict[str, object], MemoryExportProvenance | None]:
    """Project only independently verified provenance and fence its live owner evidence.

    Explicitly manual Memory remains exportable after optional evidence is removed. Every
    model-derived or candidate copy needs exact retained Chat message or document evidence.
    """
    from modules.knowledge.documents import public as documents_public
    from modules.sources import public as sources_public
    from modules.sources.schemas import SourceExportFence
    from modules.chat import public as chat_public

    raw = row.provenance if isinstance(row.provenance, dict) else {}
    allowed = {"conversation_id", "message_id", "source_id", "document_id",
               "document_version_id", "chunk_id", "origin"}
    manual_memory = isinstance(row, Memory) and row.is_manual is True
    if raw.keys() - allowed and not manual_memory:
        raise HTTPException(status_code=409, detail="Memory export cannot verify copied-content provenance")

    def optional_uuid(key: str) -> UUID | None:
        """Parse one optional provenance identifier without trusting persisted JSON types."""
        value = raw.get(key)
        if value is None:
            return None
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    conversation_id, message_id = optional_uuid("conversation_id"), optional_uuid("message_id")
    document_id, version_id, chunk_id = (
        optional_uuid("document_id"), optional_uuid("document_version_id"), optional_uuid("chunk_id"),
    )
    raw_doc_fields = any(raw.get(key) is not None for key in ("document_id", "document_version_id", "chunk_id"))
    complete_doc = all(value is not None for value in (document_id, version_id, chunk_id))
    explicit_source_id = optional_uuid("source_id")
    if not manual_memory:
        if ((raw.get("conversation_id") is not None and conversation_id is None)
                or (raw.get("message_id") is not None and message_id is None)
                or (raw.get("source_id") is not None and explicit_source_id is None)
                or (raw.get("origin") is not None
                    and (not isinstance(raw.get("origin"), str)
                         or raw.get("origin") not in {"agent", "model"}))):
            raise HTTPException(status_code=409, detail="Memory export cannot verify copied-content provenance")
    safe_provenance: dict[str, object] = {}
    source_fence_data: dict[str, object] = {}
    chat_evidence: dict[str, object] = {}
    doc_evidence: object | None = None

    if conversation_id is not None or message_id is not None:
        if conversation_id is None or message_id is None:
            if not manual_memory:
                raise HTTPException(status_code=409, detail="Memory export cannot verify partial conversation provenance")
        else:
            origin = await chat_public.read_memory_export_origin(
                session, owner_id=1, conversation_id=conversation_id, message_id=message_id,
            )
            if origin is None:
                if not manual_memory:
                    raise HTTPException(status_code=409, detail="Memory transcript evidence was removed or is not retained")
            else:
                safe_provenance.update(conversation_id=conversation_id, message_id=message_id)
                chat_evidence = {
                    "conversation_id": origin.conversation_id,
                    "message_id": origin.message_id,
                    "chat_evidence_digest": hashlib.sha256(origin.model_dump_json().encode("utf-8")).hexdigest(),
                    "chat_privacy_persisted": origin.privacy_persisted,
                    "chat_privacy_updated_at": origin.privacy_updated_at,
                }

    if raw_doc_fields:
        if not complete_doc:
            if not manual_memory:
                raise HTTPException(status_code=409, detail="Memory export cannot verify incomplete document provenance")
        else:
            try:
                refs = await documents_public.read_evidence_refs(session, [(version_id, chunk_id)])
            except ValueError:
                refs = []
            if (len(refs) == 1 and refs[0].document_id == document_id
                    and (explicit_source_id is None or refs[0].source_id == explicit_source_id)):
                doc_evidence = refs[0]
                safe_provenance.update(
                    document_id=document_id, document_version_id=version_id, chunk_id=chunk_id,
                )
            elif not manual_memory:
                raise HTTPException(status_code=409, detail="Memory document evidence was removed or is being purged")

    source_id = explicit_source_id
    if doc_evidence is not None:
        source_id = doc_evidence.source_id
    if source_id is not None:
        source = await sources_public.get_source_fence(session, source_id)
        eligible = bool(source and await sources_public.filter_export_eligible_sources(
            session, [SourceExportFence(source_id=source_id, generation=source.generation)],
        ))
        if not eligible:
            if not manual_memory:
                raise HTTPException(status_code=409, detail="Memory source evidence was removed or is being purged")
            safe_provenance.pop("source_id", None)
            if doc_evidence is not None:
                for key in ("document_id", "document_version_id", "chunk_id"):
                    safe_provenance.pop(key, None)
                doc_evidence = None
        else:
            safe_provenance["source_id"] = source_id
            source_fence_data = {"source_id": source.id, "source_generation": source.generation}

    if doc_evidence is not None and source_fence_data:
        safe_provenance.update(
            document_id=document_id, document_version_id=version_id, chunk_id=chunk_id,
        )
        source_fence_data.update(
            document_id=document_id, document_version_id=version_id, chunk_id=chunk_id,
        )
    elif doc_evidence is not None:
        source_fence_data.update(document_id=document_id, document_version_id=version_id, chunk_id=chunk_id)

    if not doc_evidence and not chat_evidence and not manual_memory:
        raise HTTPException(status_code=409, detail="Memory export cannot verify copied-content provenance")
    if manual_memory:
        safe_provenance["origin"] = "manual"
    elif isinstance(raw.get("origin"), str) and raw.get("origin") in {"agent", "model"}:
        safe_provenance["origin"] = raw["origin"]
    projected = MemoryExportProvenance.model_validate(safe_provenance) if safe_provenance else None
    return {**source_fence_data, **chat_evidence}, projected


def _memory_export_scope(record_kind: str, snapshot_at: datetime) -> tuple[object, ...]:
    """Select only records created and last changed by the immutable page cutoff."""
    if record_kind == "memories":
        return (Memory.status != "forgotten", Memory.created_at <= snapshot_at, Memory.updated_at <= snapshot_at)
    return (MemoryCandidate.created_at <= snapshot_at, MemoryCandidate.updated_at <= snapshot_at)


async def _memory_export_count(session: AsyncSession, record_kind: str, snapshot_at: datetime) -> int:
    """Count the retained owner inventory at one fixed cutoff for page and final checks."""
    model = Memory if record_kind == "memories" else MemoryCandidate
    return int(await session.scalar(
        select(func.count()).select_from(model).where(*_memory_export_scope(record_kind, snapshot_at))
    ) or 0)


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
) -> MemoryExportPage:
    """Return a bounded owner page after taking the privacy fence and validating copied evidence.

    The immutable cutoff and content digests support the export publisher's second source/owner
    fence. No arbitrary copied provenance JSON is included in the portable projection.
    """
    await lock_export_privacy(session)
    if owner_id != 1 or record_kind not in {"memories", "candidates"} or not 1 <= limit <= 100:
        raise ValueError("Memory export owner, kind or page limit is invalid")
    if await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        raise HTTPException(status_code=404, detail="Owner not found")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_memory_export_cursor(cursor, owner_id, record_kind)
        position = (position_at, position_id)
    model = Memory if record_kind == "memories" else MemoryCandidate
    statement = select(model).where(*_memory_export_scope(record_kind, snapshot_at))
    if position is not None:
        statement = statement.where(tuple_(model.created_at, model.id) > position)
    rows = list((await session.scalars(
        statement.order_by(model.created_at, model.id).limit(limit + 1)
        .execution_options(populate_existing=True)
    )).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items, fences, omitted_count = [], [], 0
    payload_bytes = 2
    for row in rows:
        try:
            provenance_fence, provenance = await _memory_export_source_fence(session, row)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            omitted_count += 1
            continue
        item = (_memory_export_read(row, provenance) if record_kind == "memories"
                else _candidate_export_read(row, provenance))
        raw = item.model_dump_json().encode("utf-8")
        proposed_bytes = payload_bytes + len(raw) + (1 if items else 0)
        if proposed_bytes > MEMORY_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("Memory export record exceeds its page byte bound")
            raise HTTPException(status_code=413, detail="Memory export page exceeds its byte bound")
        payload_bytes = proposed_bytes
        items.append(item)
        fences.append(MemoryExportFence(
            record_kind="memory" if record_kind == "memories" else "candidate", id=row.id,
            created_at=row.created_at, updated_at=row.updated_at,
            content_digest=hashlib.sha256(raw).hexdigest(), **provenance_fence,
        ))
    return MemoryExportPage(
        owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
        snapshot_count=await _memory_export_count(session, record_kind, snapshot_at),
        omitted_count=omitted_count, items=items, fences=fences, payload_bytes=payload_bytes,
        max_payload_bytes=MEMORY_EXPORT_PAGE_MAX_BYTES,
        available=omitted_count == 0,
        omission_reason="unsupported_provenance" if omitted_count else None,
        next_cursor=_encode_memory_export_cursor(owner_id, record_kind, snapshot_at, rows[-1].created_at, rows[-1].id)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[MemoryExportFence],
) -> MemoryExportFenceValidation:
    """Recheck privacy, retained owner rows, exact content and inventory before publication."""
    await lock_export_privacy(session)
    if owner_id != 1 or record_kind not in {"memories", "candidates"} or len(fences) > 100:
        raise ValueError("Memory export validation input is invalid")
    if await session.scalar(select(Owner.id).where(Owner.id == owner_id)) is None:
        return MemoryExportFenceValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    observed = await _memory_export_count(session, record_kind, snapshot_at)
    if observed != expected_snapshot_count:
        return MemoryExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    model = Memory if record_kind == "memories" else MemoryCandidate
    for fence in fences:
        row = await session.scalar(select(model).where(
            model.id == fence.id, *_memory_export_scope(record_kind, snapshot_at),
        ).execution_options(populate_existing=True))
        if row is None or row.created_at != fence.created_at or row.updated_at != fence.updated_at:
            return MemoryExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        try:
            source_fence, provenance = await _memory_export_source_fence(session, row)
        except HTTPException:
            return MemoryExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = (_memory_export_read(row, provenance) if record_kind == "memories"
                else _candidate_export_read(row, provenance))
        digest = hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()
        if digest != fence.content_digest:
            return MemoryExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        expected_source_fence = fence.model_dump(exclude={"record_kind", "id", "created_at", "updated_at", "content_digest"}, exclude_none=True)
        if source_fence != expected_source_fence:
            return MemoryExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return MemoryExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)


_PRIVACY_LOCK_NAMESPACE = 1297109577


async def lock_export_privacy(session: AsyncSession) -> None:
    """Serialize Chat consent fences with Memory privacy writes in the caller transaction.

    The transaction-scoped owner lock also fences the absent-row default against a concurrent
    privacy update that would otherwise insert the first MemoryPrivacyRecord after a Chat read.
    Callers must keep the transaction open through their guarded read/write and commit or rollback
    promptly; this never exposes Memory's ORM model across the public module boundary.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :owner_id)"),
        {"namespace": _PRIVACY_LOCK_NAMESPACE, "owner_id": 1},
    )


async def _lock_live_provenance_evidence(
    session: AsyncSession, provenance: object, *, require_copy_evidence: bool,
) -> None:
    """Acquire privacy-held live evidence locks in Source then Document order and reject stale claims.

    Callers first read a provenance hint, acquire the privacy lock, then use this helper before
    locking the copied Memory/Candidate rows and comparing the locked row to its hint.
    """
    if not isinstance(provenance, dict):
        raise HTTPException(status_code=409, detail="Memory provenance is not verifiable")
    from modules.knowledge.documents import public as documents_public
    from modules.sources import public as sources_public
    from modules.sources.schemas import SourceExportFence
    from modules.chat import public as chat_public

    raw = provenance
    if require_copy_evidence and raw.keys() - {
        "conversation_id", "message_id", "source_id", "document_id",
        "document_version_id", "chunk_id", "origin",
    }:
        raise HTTPException(status_code=409, detail="Memory provenance contains unsupported copied fields")
    source_value = raw.get("source_id")
    source_id = _provenance_uuid(source_value) if source_value is not None else None
    if source_value is not None and source_id is None:
        raise HTTPException(status_code=409, detail="Memory source provenance is not verifiable")
    document_values = [raw.get(key) for key in ("document_id", "document_version_id", "chunk_id")]
    has_document_identity = any(value is not None for value in document_values)
    document_verified = False
    if has_document_identity:
        document_id, version_id, chunk_id = (_provenance_uuid(value) for value in document_values)
        if document_id is None or version_id is None or chunk_id is None:
            raise HTTPException(status_code=409, detail="Memory document provenance is incomplete")
        refs = await documents_public.read_evidence_refs(
            session, [(version_id, chunk_id)], for_write=True,
        )
        if (len(refs) != 1 or refs[0].document_id != document_id
                or (source_id is not None and refs[0].source_id != source_id)):
            raise HTTPException(status_code=409, detail="Memory document evidence is removed or unavailable")
        source_id = refs[0].source_id
        document_verified = True
    elif source_id is not None:
        source = await sources_public.lock_source(session, source_id)
        eligible = bool(source and await sources_public.filter_export_eligible_sources(
            session, [SourceExportFence(source_id=source_id, generation=source.generation)],
        ))
        if not eligible:
            raise HTTPException(status_code=409, detail="Memory source evidence is removed or unavailable")

    conversation_value, message_value = raw.get("conversation_id"), raw.get("message_id")
    conversation_id = _provenance_uuid(conversation_value) if conversation_value is not None else None
    message_id = _provenance_uuid(message_value) if message_value is not None else None
    chat_verified = False
    if conversation_value is not None or message_value is not None:
        if conversation_id is None or message_id is None:
            raise HTTPException(status_code=409, detail="Memory conversation provenance is incomplete")
        chat_verified = await chat_public.read_memory_export_origin(
            session, owner_id=1, conversation_id=conversation_id, message_id=message_id,
        ) is not None
        if not chat_verified:
            raise HTTPException(status_code=409, detail="Memory transcript evidence is removed or unavailable")
    if require_copy_evidence and not (document_verified or chat_verified):
        raise HTTPException(status_code=409, detail="Memory copied-content evidence is not verifiable")


async def read_export_privacy(session: AsyncSession) -> MemoryExportPrivacy:
    """Read only the history-retention value and its persisted-row snapshot fence."""
    row = (await session.execute(
        select(
            MemoryPrivacyRecord.store_conversation_history,
            MemoryPrivacyRecord.updated_at,
        ).where(MemoryPrivacyRecord.owner_id == 1)
    )).one_or_none()
    if row is None:
        # Match MemoryService.get_privacy_config without inserting defaults or committing.
        return MemoryExportPrivacy(
            store_conversation_history=True, persisted=False, updated_at=None,
        )
    store_history, updated_at = row
    if (type(store_history) is not bool or not isinstance(updated_at, datetime)
            or updated_at.tzinfo is None or updated_at.utcoffset() is None):
        raise HTTPException(status_code=503, detail="Memory export privacy state is invalid")
    return MemoryExportPrivacy(
        store_conversation_history=store_history, persisted=True, updated_at=updated_at,
    )


def _document_cleanup_scope_fingerprint(scope: "DocumentCleanupEvidenceScope") -> str:
    """Bind a cursor to one immutable Documents identity page."""
    refs = sorted(
        (str(item.document_version_id), str(item.chunk_id) if item.chunk_id else "", item.reference_kind)
        for item in scope.references
    )
    encoded = json.dumps(
        [str(scope.operation_id), str(scope.source_id), str(scope.document_id), refs],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_document_memory_cleanup_cursor(
    scope: "DocumentCleanupEvidenceScope", kind: str, after: UUID | None,
) -> str:
    """Encode the operation/page/owner-kind keyset position canonically."""
    value = json.dumps(
        [str(scope.operation_id), _document_cleanup_scope_fingerprint(scope), kind,
         str(after) if after else None],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_document_memory_cleanup_cursor(
    cursor: str, scope: "DocumentCleanupEvidenceScope",
) -> tuple[str, UUID | None]:
    """Reject malformed or cross-operation/reference-page Memory continuation tokens."""
    try:
        if len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor:
            raise ValueError
        payload = json.loads(raw)
        if (not isinstance(payload, list) or len(payload) != 4
                or payload[:2] != [str(scope.operation_id), _document_cleanup_scope_fingerprint(scope)]
                or payload[2] not in {"memories", "candidates"}):
            raise ValueError
        after = UUID(payload[3]) if payload[3] is not None else None
        if (payload[3] is not None and str(after) != payload[3]
                or _encode_document_memory_cleanup_cursor(scope, payload[2], after) != cursor):
            raise ValueError
        return payload[2], after
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Memory cleanup cursor is invalid") from exc


def _provenance_uuid(value: object) -> UUID | None:
    """Parse a stored provenance UUID without coercing arbitrary values into evidence."""
    if not isinstance(value, (str, UUID)):
        return None
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed


def _document_provenance_match(
    provenance: object, scope: "DocumentCleanupEvidenceScope",
) -> tuple[bool, bool]:
    """Return exact-scope match and source-tied unresolved provenance flags."""
    if not isinstance(provenance, dict):
        # An opaque legacy payload cannot be attributed to this particular Document. Reads
        # still fail closed in the verified projection, but charging it to every deletion
        # receipt would make an unrelated malformed row permanently block all Documents.
        return False, False
    versions = {item.document_version_id for item in scope.references}
    chunks = {item.chunk_id for item in scope.references if item.chunk_id is not None}
    document_id = _provenance_uuid(provenance.get("document_id"))
    version_id = _provenance_uuid(provenance.get("document_version_id"))
    chunk_id = _provenance_uuid(provenance.get("chunk_id"))
    matched = (
        document_id == scope.document_id
        or version_id in versions
        or chunk_id in chunks
    )
    source_id = _provenance_uuid(provenance.get("source_id"))
    source_tied = source_id == scope.source_id
    has_document_fields = any(
        key in provenance for key in ("document_id", "document_version_id", "chunk_id")
    )
    has_identity_value = any(
        provenance.get(key) is not None
        for key in ("document_id", "document_version_id", "chunk_id")
    )
    malformed = (
        provenance.get("document_id") is not None and document_id is None
        or provenance.get("document_version_id") is not None and version_id is None
        or provenance.get("chunk_id") is not None and chunk_id is None
    )
    # A different valid Document UUID is explicit evidence that this copy belongs elsewhere.
    identified_elsewhere = (
        document_id is not None and document_id != scope.document_id
    ) or (
        document_id is None and version_id is not None and version_id not in versions
    )
    unsupported = bool(provenance.keys() - {
        "conversation_id", "message_id", "source_id", "document_id",
        "document_version_id", "chunk_id", "origin",
    })
    unresolved = not matched and not identified_elsewhere and source_tied and (
        malformed or not has_identity_value or unsupported
        or (has_document_fields and not all(
            provenance.get(key) is not None
            for key in ("document_id", "document_version_id", "chunk_id")
        ))
    )
    return matched, unresolved


def _scrub_manual_document_provenance(
    provenance: object, scope: "DocumentCleanupEvidenceScope",
) -> dict[str, object] | None:
    """Remove only exact revoked Document identifiers from explicitly manual provenance."""
    if not isinstance(provenance, dict):
        return None
    result = dict(provenance)
    for key, expected in (
        ("document_id", scope.document_id),
    ):
        if _provenance_uuid(result.get(key)) == expected:
            result.pop(key, None)
    versions = {item.document_version_id for item in scope.references}
    chunks = {item.chunk_id for item in scope.references if item.chunk_id is not None}
    if _provenance_uuid(result.get("document_version_id")) in versions:
        result.pop("document_version_id", None)
    if _provenance_uuid(result.get("chunk_id")) in chunks:
        result.pop("chunk_id", None)
    return result


def _scrub_derived_memory(item: Memory, now: datetime) -> None:
    """Physically erase a derived copy while retaining its stable lifecycle identity."""
    item.content = ""
    item.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
    item.provenance = {}
    item.status = "forgotten"
    item.forgotten_at = now
    item.updated_at = now


def _scrub_candidate(item: MemoryCandidate, now: datetime) -> None:
    """Physically erase a derived review copy while retaining its stable lifecycle identity."""
    item.content = ""
    item.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
    item.rejection_reason = None
    item.provenance = {}
    item.status = "expired"
    item.updated_at = now


async def purge_document_copied_evidence_page(
    session: AsyncSession,
    scope: "DocumentCleanupEvidenceScope",
    *,
    cursor: str | None,
    limit: int = 100,
) -> DocumentMemoryCleanupProgress:
    """Flush one bounded Memory/Candidate sweep for a detached deleted-Document scope.

    The caller owns the transaction, privacy lock, cursor/stage receipt, commit and postcommit
    cache eviction. This hook never reads or locks Source or Document rows; it matches only
    immutable captured IDs and changes at most ``limit`` owner records per page. A Memory page
    locks at most ``limit`` Memory rows plus its one-row lookahead and may inspect one linked
    Candidate per selected Memory. The Candidate sweep starts only after the full Memory sweep,
    preserving shared Candidate lineage until all linked derived Memory copies were examined.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Memory cleanup page size must be between 1 and 100")
    kind, after = _decode_document_memory_cleanup_cursor(cursor, scope) if cursor else ("memories", None)
    scrubbed_memories = scrubbed_candidates = unresolved = examined = 0
    now = datetime.now(UTC)

    while examined < limit and kind in {"memories", "candidates"}:
        remaining = limit - examined
        if kind == "memories":
            statement = select(Memory).order_by(Memory.id).limit(remaining + 1)
            if after is not None:
                statement = statement.where(Memory.id > after)
            rows = list((await session.scalars(statement.with_for_update())).all())
            has_more = len(rows) > remaining
            rows = rows[:remaining]
            for item in rows:
                matched, uncertain = _document_provenance_match(item.provenance, scope)
                candidate = None
                if item.candidate_id is not None:
                    candidate = await session.scalar(select(MemoryCandidate).where(
                        MemoryCandidate.id == item.candidate_id,
                    ).with_for_update().execution_options(populate_existing=True))
                candidate_match, candidate_uncertain = (
                    _document_provenance_match(candidate.provenance, scope)
                    if candidate is not None else (False, False)
                )
                if item.is_manual is True:
                    if matched:
                        scrubbed = _scrub_manual_document_provenance(item.provenance, scope)
                        if scrubbed is not None and scrubbed != item.provenance:
                            item.provenance = scrubbed
                            item.updated_at = now
                            scrubbed_memories += 1
                elif matched or candidate_match:
                    _scrub_derived_memory(item, now)
                    scrubbed_memories += 1
                elif uncertain or candidate_uncertain:
                    unresolved += 1
                examined += 1
            last = rows[-1].id if rows else after
            if has_more:
                next_cursor = _encode_document_memory_cleanup_cursor(scope, kind, last)
                break
            kind, after = "candidates", None
            next_cursor = _encode_document_memory_cleanup_cursor(scope, kind, None)
            if examined == limit:
                break
        else:
            id_statement = select(MemoryCandidate.id).order_by(MemoryCandidate.id).limit(remaining + 1)
            if after is not None:
                id_statement = id_statement.where(MemoryCandidate.id > after)
            candidate_ids = list((await session.scalars(id_statement)).all())
            has_more = len(candidate_ids) > remaining
            candidate_ids = candidate_ids[:remaining]
            for candidate_id in candidate_ids:
                candidate = await session.scalar(select(MemoryCandidate).where(
                    MemoryCandidate.id == candidate_id,
                ).with_for_update().execution_options(populate_existing=True))
                if candidate is None:
                    continue
                matched, uncertain = _document_provenance_match(candidate.provenance, scope)
                if matched:
                    _scrub_candidate(candidate, now)
                    scrubbed_candidates += 1
                elif uncertain:
                    unresolved += 1
                examined += 1
            last = candidate_ids[-1] if candidate_ids else after
            if has_more:
                next_cursor = _encode_document_memory_cleanup_cursor(scope, kind, last)
                break
            next_cursor = None
            break

    complete = next_cursor is None
    return DocumentMemoryCleanupProgress(
        complete=complete,
        next_cursor=next_cursor,
        changed=bool(scrubbed_memories or scrubbed_candidates),
        scrubbed_memories=scrubbed_memories,
        scrubbed_candidates=scrubbed_candidates,
        unresolved_count=unresolved,
        unresolved_reason=_DOCUMENT_MEMORY_UNRESOLVED_REASON if unresolved else None,
    )


async def invalidate_memory_cache(redis: Redis) -> None:
    """Evict active Memory data after cleanup commit; propagate failure for receipt retry."""
    await redis.delete(CACHE_KEY_MEMORIES_ACTIVE)


def _to_memory_read(item: Memory, provenance: dict[str, Any] | None = None) -> MemoryRead:
    """Project a Memory ORM instance into a safe MemoryRead schema.

    Args:
        item: Memory ORM model instance.
        provenance: Optional owner-verified provenance projection.

    Returns:
        MemoryRead schema instance.
    """
    return MemoryRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=provenance if provenance is not None else (item.provenance or {}),
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


async def _verified_memory_read(
    session: AsyncSession, item: Memory,
) -> MemoryRead | None:
    """Return a current-evidence projection or suppress a copied record whose origin is unverified."""
    try:
        await lock_export_privacy(session)
        if item.is_manual is not True:
            await _lock_live_provenance_evidence(
                session, item.provenance, require_copy_evidence=True,
            )
        elif isinstance(item.provenance, dict):
            try:
                await _lock_live_provenance_evidence(
                    session, item.provenance, require_copy_evidence=False,
                )
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
        _, provenance = await _memory_export_source_fence(session, item)
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise
    return _to_memory_read(
        item,
        provenance.model_dump(mode="json", exclude_none=True) if provenance else {},
    )


async def _verified_candidate_read(
    session: AsyncSession, item: MemoryCandidate,
) -> MemoryCandidateRead | None:
    """Return a candidate only while its copied evidence still has an owner-valid projection."""
    try:
        await lock_export_privacy(session)
        await _lock_live_provenance_evidence(
            session, item.provenance, require_copy_evidence=True,
        )
        _, provenance = await _memory_export_source_fence(session, item)
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise
    return _to_candidate_read(
        item,
        provenance.model_dump(mode="json", exclude_none=True) if provenance else {},
    )


def _to_candidate_read(
    item: MemoryCandidate, provenance: dict[str, Any] | None = None,
) -> MemoryCandidateRead:
    """Project a MemoryCandidate ORM instance into a safe MemoryCandidateRead schema.

    Args:
        item: MemoryCandidate ORM model instance.
        provenance: Optional owner-verified provenance projection.

    Returns:
        MemoryCandidateRead schema instance.
    """
    return MemoryCandidateRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=provenance if provenance is not None else (item.provenance or {}),
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


class MemoryService:
    """Service managing memory lifecycle, candidates, evaluation, and privacy settings."""

    def __init__(self, session: AsyncSession, redis: Redis | None = None) -> None:
        """Bind active database session and optional cache client.

        Args:
            session: Active asynchronous SQLAlchemy database session.
            redis: Optional Redis client for cache eviction.
        """
        self.session = session
        self.redis = redis

    async def _invalidate_cache(self) -> None:
        """Evict active memory cache key if Redis client is available."""
        if self.redis is not None:
            try:
                await self.redis.delete(CACHE_KEY_MEMORIES_ACTIVE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to evict memory cache: %s", exc)

    async def get_memories(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        memory_type: str | None = None,
        status: str = "active",
        query: str | None = None,
    ) -> MemoryPage:
        """Retrieve cursor-paginated memory items matching filter criteria.

        Excluded forgotten or invalidated memories when status is 'active'.

        Args:
            limit: Maximum items to return, clamped to 1-100.
            cursor: Opaque pagination cursor.
            memory_type: Filter by 'fact', 'preference', or 'instruction'.
            status: Filter by status ('active', 'invalidated', 'superseded', 'forgotten').
            query: Substring search filter against content.

        Returns:
            MemoryPage with items list and next_cursor.
        """
        clamped_limit = max(1, min(limit, 100))
        stmt = select(Memory).where(Memory.status == status)

        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if query:
            stmt = stmt.where(Memory.content.ilike(f"%{query.strip()}%"))

        if cursor:
            created_at, identifier = decode_cursor(cursor)
            stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < (created_at, identifier))

        await lock_export_privacy(self.session)
        rows = list((await self.session.scalars(
            stmt.order_by(desc(Memory.created_at), desc(Memory.id)).limit(101)
        )).all())
        items: list[MemoryRead] = []
        examined = 0
        for row in rows[:100]:
            examined += 1
            projected = await _verified_memory_read(self.session, row)
            if projected is not None:
                items.append(projected)
                if len(items) == clamped_limit:
                    break
        has_more = examined < len(rows) or len(rows) > 100
        next_cursor = (
            encode_cursor(rows[examined - 1].created_at, rows[examined - 1].id)
            if has_more and examined else None
        )

        return MemoryPage(
            items=items,
            next_cursor=next_cursor,
        )

    async def get_memory(self, memory_id: UUID) -> MemoryRead | None:
        """Fetch a single memory item by identifier.

        Args:
            memory_id: UUID of the target memory.

        Returns:
            MemoryRead if found, None otherwise.
        """
        await lock_export_privacy(self.session)
        item = await self.session.get(Memory, memory_id)
        return await _verified_memory_read(self.session, item) if item is not None else None

    async def create_memory(
        self,
        payload: MemoryCreate,
        *,
        is_manual: bool = True,
        candidate_id: UUID | None = None,
    ) -> MemoryRead:
        """Explicitly create an owner memory or persist an accepted candidate.

        Args:
            payload: Validated MemoryCreate schema.
            is_manual: True if explicitly created by user, False if model-derived.
            candidate_id: Optional reference to the origin MemoryCandidate.

        Returns:
            Newly created MemoryRead.
        """
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, payload.provenance, require_copy_evidence=not is_manual,
        )
        now = datetime.now(UTC)
        prov = dict(payload.provenance)
        if is_manual:
            prov["origin"] = "manual"

        item = Memory(
            content=payload.content.strip(),
            memory_type=payload.type,
            provenance=prov,
            confidence=payload.confidence,
            reason=payload.reason,
            status="active",
            is_manual=is_manual,
            candidate_id=candidate_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        projected = await _verified_memory_read(self.session, item)
        if projected is None:
            raise HTTPException(status_code=409, detail="Memory evidence is removed or unavailable")
        return projected

    async def update_memory(self, memory_id: UUID, payload: MemoryUpdate) -> MemoryRead | None:
        """Update an existing active memory's content, type, or reason.

        Args:
            memory_id: Target memory identifier.
            payload: Validated MemoryUpdate schema.

        Returns:
            Updated MemoryRead, or None if not found or not active.
        """
        hint = await self.session.execute(
            select(Memory.id, Memory.provenance).where(Memory.id == memory_id)
        )
        hinted = hint.one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance,
            require_copy_evidence=bool(await self.session.scalar(
                select(Memory.is_manual).where(Memory.id == memory_id)
            ) is False),
        )
        item = await self.session.scalar(select(Memory).where(
            Memory.id == memory_id,
        ).with_for_update().execution_options(populate_existing=True))
        if item is None or item.status != "active":
            return None
        if item.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Memory evidence changed while it was being updated")

        now = datetime.now(UTC)
        if payload.content is not None:
            item.content = payload.content.strip()
        if payload.type is not None:
            item.memory_type = payload.type
        if payload.confidence is not None:
            item.confidence = payload.confidence
        if payload.reason is not None:
            item.reason = payload.reason
        item.updated_at = now

        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        return await _verified_memory_read(self.session, item)

    async def forget_memory(self, memory_id: UUID, *, reason: str | None = None) -> MemoryRead | None:
        """Immediately mark a memory forgotten, removing it from retrieval and purging candidate links.

        Makes content unavailable to retrieval immediately. Durable deletion cleans
        candidate references and evicts memory caches.

        Args:
            memory_id: UUID of the memory to forget.
            reason: Optional explanation for forgetting.

        Returns:
            Forgotten MemoryRead, or None if not found.
        """
        hinted = (await self.session.execute(
            select(Memory.provenance, Memory.is_manual).where(Memory.id == memory_id)
        )).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance, require_copy_evidence=hinted.is_manual is False,
        )
        item = await self.session.scalar(select(Memory).where(
            Memory.id == memory_id,
        ).with_for_update().execution_options(populate_existing=True))
        if item is None:
            return None
        if item.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Memory evidence changed while it was being forgotten")

        now = datetime.now(UTC)
        item.status = "forgotten"
        item.forgotten_at = now
        item.updated_at = now
        if reason:
            item.reason = reason

        # Purge linked candidate payload containing evidence
        if item.candidate_id is not None:
            cand = await self.session.scalar(select(MemoryCandidate).where(
                MemoryCandidate.id == item.candidate_id,
            ).with_for_update().execution_options(populate_existing=True))
            if cand is not None:
                cand.status = "superseded"
                cand.rejection_reason = "Parent memory forgotten by owner"
                cand.content = ""
                cand.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
                cand.provenance = {}

        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        projected = await _verified_memory_read(self.session, item)
        if projected is None:
            raise HTTPException(status_code=409, detail="Memory evidence is removed or unavailable")
        return projected

    async def invalidate_memory(self, memory_id: UUID, *, reason: str) -> MemoryRead | None:
        """Mark an active memory invalidated due to factual inaccuracy or policy.

        Args:
            memory_id: Target memory identifier.
            reason: Required rationale explaining why memory is invalid.

        Returns:
            Invalidated MemoryRead, or None if not found.
        """
        hinted = (await self.session.execute(
            select(Memory.provenance, Memory.is_manual).where(Memory.id == memory_id)
        )).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance, require_copy_evidence=hinted.is_manual is False,
        )
        item = await self.session.scalar(select(Memory).where(
            Memory.id == memory_id,
        ).with_for_update().execution_options(populate_existing=True))
        if item is None:
            return None
        if item.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Memory evidence changed while it was being invalidated")

        now = datetime.now(UTC)
        item.status = "invalidated"
        item.invalidated_at = now
        item.updated_at = now
        item.reason = reason

        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        projected = await _verified_memory_read(self.session, item)
        if projected is None:
            raise HTTPException(status_code=409, detail="Memory evidence is removed or unavailable")
        return projected

    async def supersede_memory(
        self,
        memory_id: UUID,
        payload: MemorySupersedeRequest,
    ) -> tuple[MemoryRead, MemoryRead] | None:
        """Supersede an existing memory with newer, updated knowledge.

        Marks old memory as 'superseded' and creates a new active memory linked to it. A derived
        predecessor yields a derived replacement with the same exact provenance and candidate
        lineage; only an explicitly manual predecessor yields an independent manual replacement.

        Args:
            memory_id: Existing memory identifier to supersede.
            payload: Validated MemorySupersedeRequest.

        Returns:
            Tuple of (superseded_old_memory, new_replacement_memory), or None if not found.
        """
        hinted = (await self.session.execute(
            select(Memory.provenance, Memory.is_manual).where(Memory.id == memory_id)
        )).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance, require_copy_evidence=hinted.is_manual is False,
        )
        old_item = await self.session.scalar(select(Memory).where(
            Memory.id == memory_id,
        ).with_for_update().execution_options(populate_existing=True))
        if old_item is None:
            return None
        if old_item.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Memory evidence changed while it was being superseded")

        now = datetime.now(UTC)
        derived_replacement = old_item.is_manual is False
        replacement_provenance = (
            dict(old_item.provenance) if isinstance(old_item.provenance, dict)
            else old_item.provenance
        ) if derived_replacement else {"supersedes": str(old_item.id), "origin": "manual"}
        new_item = Memory(
            content=payload.new_content.strip(),
            memory_type=payload.type or old_item.memory_type,
            # The lifecycle FK below records supersession. Derived copies must retain their
            # exact evidence and candidate lineage so later revocation can still find them.
            provenance=replacement_provenance,
            confidence=payload.confidence,
            reason=payload.reason,
            status="active",
            is_manual=not derived_replacement,
            candidate_id=old_item.candidate_id if derived_replacement else None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_item)
        await self.session.flush()

        old_item.status = "superseded"
        old_item.superseded_by_id = new_item.id
        old_item.updated_at = now

        await self.session.commit()
        await self.session.refresh(old_item)
        await self.session.refresh(new_item)
        await self._invalidate_cache()

        old_read = await _verified_memory_read(self.session, old_item)
        new_read = await _verified_memory_read(self.session, new_item)
        if old_read is None or new_read is None:
            raise HTTPException(status_code=409, detail="Memory evidence is removed or unavailable")
        return old_read, new_read

    async def get_privacy_config(self) -> MemoryPrivacyConfig:
        """Read owner memory and conversation privacy settings, initializing defaults if needed.

        Returns:
            MemoryPrivacyConfig with store_conversation_history, store_agent_memory, auto_accept_memory.
        """
        rec = await self.session.get(MemoryPrivacyRecord, 1)
        if rec is None:
            # Some read routes create these defaults, so fence the insert just like
            # a user write before it becomes part of the snapshot boundary.
            from modules.settings.public import admit_write

            await admit_write(self.session, "memory_privacy_default")
            # Serialize first-default insertion with consent updates and Chat's absent-row fence.
            await lock_export_privacy(self.session)
            rec = await self.session.scalar(
                select(MemoryPrivacyRecord).where(MemoryPrivacyRecord.owner_id == 1)
                .with_for_update().execution_options(populate_existing=True)
            )
        if rec is None:
            rec = MemoryPrivacyRecord(
                owner_id=1,
                store_conversation_history=True,
                store_agent_memory=False,
                auto_accept_memory=False,
            )
            self.session.add(rec)
            await self.session.commit()
            await self.session.refresh(rec)

        return MemoryPrivacyConfig(
            store_conversation_history=rec.store_conversation_history,
            store_agent_memory=rec.store_agent_memory,
            auto_accept_memory=rec.auto_accept_memory,
        )

    async def _read_privacy_config_under_fence(self) -> MemoryPrivacyConfig:
        """Read fresh consent while holding the privacy lock through the caller's decision.

        The default-row initializer admits writes before acquiring this owner lock and may
        commit internally. If the fresh locked read finds no row, release its read transaction,
        initialize through that existing admission path, then reacquire the privacy fence and
        reread with populate_existing before the caller acts on consent.
        """
        await lock_export_privacy(self.session)
        rec = await self.session.scalar(
            select(MemoryPrivacyRecord).where(MemoryPrivacyRecord.owner_id == 1)
            .with_for_update().execution_options(populate_existing=True)
        )
        if rec is None:
            # Do not acquire the backup admission barrier after the privacy owner lock.
            await self.session.rollback()
            await self.get_privacy_config()
            await lock_export_privacy(self.session)
            rec = await self.session.scalar(
                select(MemoryPrivacyRecord).where(MemoryPrivacyRecord.owner_id == 1)
                .with_for_update().execution_options(populate_existing=True)
            )
        if rec is None:
            raise HTTPException(status_code=503, detail="Memory privacy settings are unavailable")
        return MemoryPrivacyConfig(
            store_conversation_history=rec.store_conversation_history,
            store_agent_memory=rec.store_agent_memory,
            auto_accept_memory=rec.auto_accept_memory,
        )

    async def update_privacy_config(self, payload: MemoryPrivacyUpdate) -> MemoryPrivacyConfig:
        """Update owner memory privacy controls.

        Args:
            payload: Validated MemoryPrivacyUpdate fields.

        Returns:
            Updated MemoryPrivacyConfig.
        """
        # Consent changes share the same xact lock Chat holds through admission/publication.
        await lock_export_privacy(self.session)
        rec = await self.session.scalar(
            select(MemoryPrivacyRecord).where(MemoryPrivacyRecord.owner_id == 1)
            .with_for_update().execution_options(populate_existing=True)
        )
        if rec is None:
            rec = MemoryPrivacyRecord(owner_id=1)
            self.session.add(rec)

        if payload.store_conversation_history is not None:
            rec.store_conversation_history = payload.store_conversation_history
        if payload.store_agent_memory is not None:
            rec.store_agent_memory = payload.store_agent_memory
        if payload.auto_accept_memory is not None:
            rec.auto_accept_memory = payload.auto_accept_memory
        rec.updated_at = datetime.now(UTC)

        await self.session.commit()
        await self.session.refresh(rec)

        return MemoryPrivacyConfig(
            store_conversation_history=rec.store_conversation_history,
            store_agent_memory=rec.store_agent_memory,
            auto_accept_memory=rec.auto_accept_memory,
        )

    async def evaluate_novelty(
        self,
        content: str,
        memory_type: str = "fact",
    ) -> dict[str, Any]:
        """Evaluate content novelty and usefulness against active memories.

        Args:
            content: Proposed memory text.
            memory_type: Type of memory.

        Returns:
            Dictionary containing novelty_score, usefulness_score, confidence, and recommendation.
        """
        privacy = await self._read_privacy_config_under_fence()
        source_rows = list((await self.session.scalars(
            select(Memory).where(Memory.status == "active")
            .order_by(desc(Memory.confidence), desc(Memory.created_at)).limit(100)
        )).all())
        active_contents: list[str] = []
        for row in source_rows:
            if await _verified_memory_read(self.session, row) is not None:
                active_contents.append(row.content)
        evaluation = evaluate_candidate(
            content,
            memory_type,
            active_contents,
            auto_accept_enabled=privacy.auto_accept_memory,
        )

        return {
            "novelty_score": evaluation.novelty_score,
            "usefulness_score": evaluation.usefulness_score,
            "confidence_score": evaluation.confidence_score,
            "reason": evaluation.reason,
            "is_duplicate": evaluation.is_duplicate,
            "should_auto_accept": evaluation.should_auto_accept,
        }

    async def suggest_candidates(
        self,
        text: str,
        *,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> list[MemoryCandidateRead]:
        """Extract memory candidate proposals from text, evaluate them, and persist candidates.

        If store_agent_memory is disabled in privacy settings, candidate suggestions are skipped.
        If auto_accept_memory is enabled, passing candidates are immediately converted into active memories.

        Args:
            text: Utterance or conversation content to scan.
            conversation_id: Origin conversation UUID.
            message_id: Origin message UUID.
            provenance: Additional provenance attributes.

        Returns:
            List of created MemoryCandidateRead objects.
        """
        privacy = await self._read_privacy_config_under_fence()
        if not privacy.store_agent_memory:
            return []

        proposals = extract_candidate_proposals(text)
        if not proposals:
            return []

        prov = dict(provenance or {})
        if conversation_id:
            prov["conversation_id"] = str(conversation_id)
        if message_id:
            prov["message_id"] = str(message_id)
        prov["origin"] = "agent"
        await _lock_live_provenance_evidence(
            self.session, prov, require_copy_evidence=True,
        )

        source_rows = list((await self.session.scalars(
            select(Memory).where(Memory.status == "active")
            .order_by(desc(Memory.confidence), desc(Memory.created_at)).limit(100)
        )).all())
        active_contents: list[str] = []
        for row in source_rows:
            if await _verified_memory_read(self.session, row) is not None:
                active_contents.append(row.content)

        now = datetime.now(UTC)
        results: list[MemoryCandidateRead] = []

        for p in proposals:
            evaluation = evaluate_candidate(
                p["content"],
                p["type"],
                active_contents,
                auto_accept_enabled=privacy.auto_accept_memory,
            )

            # Skip obvious duplicates
            if evaluation.is_duplicate:
                continue

            cand = MemoryCandidate(
                content=p["content"],
                memory_type=p["type"],
                provenance=prov,
                confidence=evaluation.confidence_score,
                novelty_score=evaluation.novelty_score,
                usefulness_score=evaluation.usefulness_score,
                reason=evaluation.reason,
                status="accepted" if evaluation.should_auto_accept else "pending",
                created_at=now,
                updated_at=now,
                evaluated_at=now,
            )
            self.session.add(cand)
            await self.session.flush()

            # If auto-accepted, create active Memory immediately
            if evaluation.should_auto_accept:
                mem = Memory(
                    content=cand.content,
                    memory_type=cand.memory_type,
                    provenance=prov,
                    confidence=cand.confidence,
                    reason=cand.reason,
                    status="active",
                    is_manual=False,
                    candidate_id=cand.id,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(mem)

            results.append(_to_candidate_read(cand))

        await self.session.commit()
        await self._invalidate_cache()
        safe_results: list[MemoryCandidateRead] = []
        for result in results:
            row = await self.session.get(MemoryCandidate, result.id)
            if row is not None:
                projected = await _verified_candidate_read(self.session, row)
                if projected is not None:
                    safe_results.append(projected)
        return safe_results

    async def get_candidates(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: str = "pending",
    ) -> MemoryCandidatePage:
        """List cursor-paginated memory candidates.

        Args:
            limit: Maximum candidates to return.
            cursor: Pagination cursor.
            status: Status filter ('pending', 'accepted', 'rejected').

        Returns:
            MemoryCandidatePage.
        """
        clamped_limit = max(1, min(limit, 100))
        stmt = select(MemoryCandidate).where(MemoryCandidate.status == status)

        if cursor:
            created_at, identifier = decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(MemoryCandidate.created_at, MemoryCandidate.id) < (created_at, identifier)
            )

        await lock_export_privacy(self.session)
        stmt = stmt.order_by(
            desc(MemoryCandidate.created_at), desc(MemoryCandidate.id)
        ).limit(101)
        rows = list((await self.session.scalars(stmt)).all())
        items: list[MemoryCandidateRead] = []
        examined = 0
        for row in rows[:100]:
            examined += 1
            projected = await _verified_candidate_read(self.session, row)
            if projected is not None:
                items.append(projected)
                if len(items) == clamped_limit:
                    break
        has_more = examined < len(rows) or len(rows) > 100
        next_cursor = (
            encode_cursor(rows[examined - 1].created_at, rows[examined - 1].id)
            if has_more and examined else None
        )

        return MemoryCandidatePage(
            items=items,
            next_cursor=next_cursor,
        )

    async def accept_candidate(self, candidate_id: UUID) -> MemoryRead | None:
        """Accept a pending candidate into active owner memory.

        Args:
            candidate_id: UUID of the candidate.

        Returns:
            Created MemoryRead, or None if candidate not found.
        """
        hinted = (await self.session.execute(
            select(MemoryCandidate.provenance, MemoryCandidate.status)
            .where(MemoryCandidate.id == candidate_id)
        )).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance, require_copy_evidence=True,
        )
        cand = await self.session.scalar(select(MemoryCandidate).where(
            MemoryCandidate.id == candidate_id,
        ).with_for_update().execution_options(populate_existing=True))
        if cand is None or cand.status != "pending":
            return None
        if cand.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Candidate evidence changed while it was being accepted")
        if await _verified_candidate_read(self.session, cand) is None:
            raise HTTPException(status_code=409, detail="Candidate evidence is removed or unavailable")

        now = datetime.now(UTC)
        cand.status = "accepted"
        cand.updated_at = now

        mem = Memory(
            content=cand.content,
            memory_type=cand.memory_type,
            provenance=cand.provenance,
            confidence=cand.confidence,
            reason=cand.reason or "Explicitly accepted by owner",
            status="active",
            is_manual=False,
            candidate_id=cand.id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(mem)
        await self.session.commit()
        await self.session.refresh(mem)
        await self._invalidate_cache()
        projected = await _verified_memory_read(self.session, mem)
        if projected is None:
            raise HTTPException(status_code=409, detail="Candidate evidence is removed or unavailable")
        return projected

    async def reject_candidate(
        self, candidate_id: UUID, *, reason: str | None = None
    ) -> MemoryCandidateRead | None:
        """Reject a pending memory candidate.

        Args:
            candidate_id: UUID of candidate to reject.
            reason: Optional explanation.

        Returns:
            Updated MemoryCandidateRead, or None if not found.
        """
        hinted = (await self.session.execute(
            select(MemoryCandidate.provenance).where(MemoryCandidate.id == candidate_id)
        )).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session)
        await _lock_live_provenance_evidence(
            self.session, hinted.provenance, require_copy_evidence=True,
        )
        cand = await self.session.scalar(select(MemoryCandidate).where(
            MemoryCandidate.id == candidate_id,
        ).with_for_update().execution_options(populate_existing=True))
        if cand is None:
            return None
        if cand.provenance != hinted.provenance:
            raise HTTPException(status_code=409, detail="Candidate evidence changed while it was being rejected")

        now = datetime.now(UTC)
        cand.status = "rejected"
        cand.rejection_reason = reason
        cand.updated_at = now

        await self.session.commit()
        await self.session.refresh(cand)
        projected = await _verified_candidate_read(self.session, cand)
        if projected is None:
            raise HTTPException(status_code=409, detail="Candidate evidence is removed or unavailable")
        return projected

    async def purge_memories(self, options: MemoryPurgeRequest) -> MemoryPurgeResponse:
        """Perform durable cleanup of forgotten memories, rejected candidates, or conversation history.

        Args:
            options: MemoryPurgeRequest flags.

        Returns:
            MemoryPurgeResponse with counts of deleted records.
        """
        # Establish the global privacy-before-Memory-row lock order for this transaction.
        await lock_export_privacy(self.session)

        purged_memories = 0
        purged_candidates = 0
        purged_conversations = 0

        if options.purge_forgotten_memories:
            stmt = delete(Memory).where(Memory.status == "forgotten")
            res = await self.session.execute(stmt)
            purged_memories = res.rowcount or 0

        if options.purge_rejected_candidates:
            stmt = delete(MemoryCandidate).where(
                MemoryCandidate.status.in_(["rejected", "expired", "superseded"])
            )
            res = await self.session.execute(stmt)
            purged_candidates = res.rowcount or 0

        if options.purge_conversation_history:
            from modules.chat.public import purge_unpinned_conversations

            purged_conversations = await purge_unpinned_conversations(
                self.session, owner_id=1,
            )

        await self.session.commit()
        await self._invalidate_cache()

        return MemoryPurgeResponse(
            purged_memories_count=purged_memories,
            purged_candidates_count=purged_candidates,
            purged_conversations_count=purged_conversations,
        )

    async def get_active_memory_context(self, *, limit: int = 20) -> list[MemoryRead]:
        """Fetch active memories formatted for prompt context injection.

        Excluded forgotten or invalidated items. Bounded to specified limit.

        Args:
            limit: Maximum items to return (default 20).

        Returns:
            List of active MemoryRead objects.
        """
        privacy = await self._read_privacy_config_under_fence()
        # If agent memory is explicitly disabled, do not inject memories into prompt
        if not privacy.store_agent_memory:
            return []

        stmt = (
            select(Memory)
            .where(Memory.status == "active")
            .order_by(desc(Memory.confidence), desc(Memory.created_at))
            .limit(101)
        )
        rows = list((await self.session.scalars(stmt)).all())
        result: list[MemoryRead] = []
        for row in rows[:100]:
            projected = await _verified_memory_read(self.session, row)
            if projected is not None:
                result.append(projected)
                if len(result) >= max(1, min(limit, 50)):
                    break
        return result
