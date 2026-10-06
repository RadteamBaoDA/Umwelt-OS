"""Public contract and service interface for selective memory, candidates, and privacy management."""

from datetime import UTC, datetime
import base64
import binascii
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


def _memory_export_read(item: Memory) -> MemoryExportRead:
    """Project one retained memory record without its arbitrary JSON provenance keys."""
    return MemoryExportRead(
        id=item.id, content=item.content, type=item.memory_type,
        provenance=_memory_export_provenance(item.provenance), confidence=item.confidence,
        reason=item.reason, status=item.status, is_manual=item.is_manual,
        superseded_by_id=item.superseded_by_id, candidate_id=item.candidate_id,
        created_at=item.created_at, updated_at=item.updated_at,
        invalidated_at=item.invalidated_at, forgotten_at=item.forgotten_at,
    )


def _candidate_export_read(item: MemoryCandidate) -> MemoryCandidateExportRead:
    """Project one retained review candidate without arbitrary JSON provenance keys."""
    return MemoryCandidateExportRead(
        id=item.id, content=item.content, type=item.memory_type,
        provenance=_memory_export_provenance(item.provenance), confidence=item.confidence,
        novelty_score=item.novelty_score, usefulness_score=item.usefulness_score,
        reason=item.reason, status=item.status, rejection_reason=item.rejection_reason,
        created_at=item.created_at, updated_at=item.updated_at, evaluated_at=item.evaluated_at,
    )


async def _memory_export_source_fence(
    session: AsyncSession, row: Memory | MemoryCandidate,
) -> dict[str, object]:
    """Require exact retained owner evidence for copied content and capture source generation.

    Manual owner content without a document/source link remains independently exportable. A
    model-derived copy with unsupported, partial, removed, or purging provenance fails closed.
    """
    from modules.knowledge.documents import public as documents_public
    from modules.sources import public as sources_public
    from modules.sources.schemas import SourceExportFence

    raw = row.provenance if isinstance(row.provenance, dict) else {}
    allowed = {"source", "conversation_id", "message_id", "source_id", "document_id",
               "document_version_id", "chunk_id", "origin"}
    provenance = _memory_export_provenance(raw)
    is_manual = row.is_manual if isinstance(row, Memory) else raw.get("origin") == "manual"
    doc_keys = ("document_id", "document_version_id", "chunk_id")
    has_any_doc = any(raw.get(key) is not None for key in doc_keys)
    if has_any_doc and not all(raw.get(key) is not None for key in doc_keys):
        raise HTTPException(status_code=409, detail="Memory export cannot verify incomplete document provenance")
    linked_doc = has_any_doc
    source_value = raw.get("source_id")
    if source_value is not None and provenance is None:
        raise HTTPException(status_code=409, detail="Memory export cannot verify source provenance")
    source_id = provenance.source_id if provenance else None
    document_id = provenance.document_id if provenance else None
    version_id = provenance.document_version_id if provenance else None
    chunk_id = provenance.chunk_id if provenance else None
    if linked_doc:
        if document_id is None or version_id is None or chunk_id is None:
            raise HTTPException(status_code=409, detail="Memory export cannot verify document provenance")
        try:
            refs = await documents_public.read_evidence_refs(session, [(version_id, chunk_id)])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Memory evidence was removed or is being purged") from exc
        if (len(refs) != 1 or refs[0].document_id != document_id
                or source_id is not None and refs[0].source_id != source_id):
            raise HTTPException(status_code=409, detail="Memory evidence was removed or is being purged")
        source_id = refs[0].source_id
    if not linked_doc and not is_manual:
        raise HTTPException(status_code=409, detail="Memory export cannot verify copied-content provenance")
    if raw.keys() - allowed and not is_manual:
        raise HTTPException(status_code=409, detail="Memory export cannot verify copied-content provenance")
    if source_id is None:
        return {}
    source = await sources_public.get_source_fence(session, source_id)
    if source is None or not await sources_public.filter_export_eligible_sources(
        session, [SourceExportFence(source_id=source_id, generation=source.generation)],
    ):
        raise HTTPException(status_code=409, detail="Memory source evidence was removed or is being purged")
    return {key: value for key, value in {
        "source_id": source.id,
        "source_generation": source.generation,
        "document_id": document_id,
        "document_version_id": version_id,
        "chunk_id": chunk_id,
    }.items() if value is not None}


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
    """Return a bounded owner memory/candidate page with a fixed cutoff and content fences."""
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
    items = [_memory_export_read(row) if record_kind == "memories" else _candidate_export_read(row) for row in rows]
    item_bytes = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, item_bytes)) + max(0, len(items) - 1)
    if payload_bytes > MEMORY_EXPORT_PAGE_MAX_BYTES:
        raise ValueError("Memory export page exceeds its byte bound")
    fences = []
    for row, raw in zip(rows, item_bytes, strict=True):
        provenance_fence = await _memory_export_source_fence(session, row)
        fences.append(MemoryExportFence(
            record_kind="memory" if record_kind == "memories" else "candidate", id=row.id,
            created_at=row.created_at, updated_at=row.updated_at,
            content_digest=hashlib.sha256(raw).hexdigest(), **provenance_fence,
        ))
    return MemoryExportPage(
        owner_id=owner_id, record_kind=record_kind, snapshot_at=snapshot_at,
        snapshot_count=await _memory_export_count(session, record_kind, snapshot_at),
        items=items, fences=fences, payload_bytes=payload_bytes,
        max_payload_bytes=MEMORY_EXPORT_PAGE_MAX_BYTES,
        next_cursor=_encode_memory_export_cursor(owner_id, record_kind, snapshot_at, rows[-1].created_at, rows[-1].id)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[MemoryExportFence],
) -> MemoryExportFenceValidation:
    """Recheck retained owner rows, exact portable content and inventory count before publication."""
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
        item = _memory_export_read(row) if record_kind == "memories" else _candidate_export_read(row)
        digest = hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest()
        if digest != fence.content_digest:
            return MemoryExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        try:
            source_fence = await _memory_export_source_fence(session, row)
        except HTTPException:
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


def _to_memory_read(item: Memory) -> MemoryRead:
    """Project a Memory ORM instance into a safe MemoryRead schema.

    Args:
        item: Memory ORM model instance.

    Returns:
        MemoryRead schema instance.
    """
    return MemoryRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=item.provenance or {},
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


def _to_candidate_read(item: MemoryCandidate) -> MemoryCandidateRead:
    """Project a MemoryCandidate ORM instance into a safe MemoryCandidateRead schema.

    Args:
        item: MemoryCandidate ORM model instance.

    Returns:
        MemoryCandidateRead schema instance.
    """
    return MemoryCandidateRead(
        id=item.id,
        content=item.content,
        type=item.memory_type,
        provenance=item.provenance or {},
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

        stmt = stmt.order_by(desc(Memory.created_at), desc(Memory.id)).limit(clamped_limit + 1)
        rows = list((await self.session.scalars(stmt)).all())

        has_more = len(rows) > clamped_limit
        items = rows[:clamped_limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        )

        return MemoryPage(
            items=[_to_memory_read(r) for r in items],
            next_cursor=next_cursor,
        )

    async def get_memory(self, memory_id: UUID) -> MemoryRead | None:
        """Fetch a single memory item by identifier.

        Args:
            memory_id: UUID of the target memory.

        Returns:
            MemoryRead if found, None otherwise.
        """
        item = await self.session.get(Memory, memory_id)
        return _to_memory_read(item) if item is not None else None

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
        return _to_memory_read(item)

    async def update_memory(self, memory_id: UUID, payload: MemoryUpdate) -> MemoryRead | None:
        """Update an existing active memory's content, type, or reason.

        Args:
            memory_id: Target memory identifier.
            payload: Validated MemoryUpdate schema.

        Returns:
            Updated MemoryRead, or None if not found or not active.
        """
        item = await self.session.get(Memory, memory_id)
        if item is None or item.status != "active":
            return None

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
        return _to_memory_read(item)

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
        item = await self.session.get(Memory, memory_id)
        if item is None:
            return None

        now = datetime.now(UTC)
        item.status = "forgotten"
        item.forgotten_at = now
        item.updated_at = now
        if reason:
            item.reason = reason

        # Purge linked candidate payload containing evidence
        if item.candidate_id is not None:
            cand = await self.session.get(MemoryCandidate, item.candidate_id)
            if cand is not None:
                cand.status = "superseded"
                cand.rejection_reason = "Parent memory forgotten by owner"
                cand.provenance = {}

        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        return _to_memory_read(item)

    async def invalidate_memory(self, memory_id: UUID, *, reason: str) -> MemoryRead | None:
        """Mark an active memory invalidated due to factual inaccuracy or policy.

        Args:
            memory_id: Target memory identifier.
            reason: Required rationale explaining why memory is invalid.

        Returns:
            Invalidated MemoryRead, or None if not found.
        """
        item = await self.session.get(Memory, memory_id)
        if item is None:
            return None

        now = datetime.now(UTC)
        item.status = "invalidated"
        item.invalidated_at = now
        item.updated_at = now
        item.reason = reason

        await self.session.commit()
        await self.session.refresh(item)
        await self._invalidate_cache()
        return _to_memory_read(item)

    async def supersede_memory(
        self,
        memory_id: UUID,
        payload: MemorySupersedeRequest,
    ) -> tuple[MemoryRead, MemoryRead] | None:
        """Supersede an existing memory with newer, updated knowledge.

        Marks old memory as 'superseded' and creates a new active memory linked to it.

        Args:
            memory_id: Existing memory identifier to supersede.
            payload: Validated MemorySupersedeRequest.

        Returns:
            Tuple of (superseded_old_memory, new_replacement_memory), or None if not found.
        """
        old_item = await self.session.get(Memory, memory_id)
        if old_item is None:
            return None

        now = datetime.now(UTC)
        new_item = Memory(
            content=payload.new_content.strip(),
            memory_type=payload.type or old_item.memory_type,
            provenance={"supersedes": str(old_item.id), "origin": "manual"},
            confidence=payload.confidence,
            reason=payload.reason,
            status="active",
            is_manual=True,
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

        return _to_memory_read(old_item), _to_memory_read(new_item)

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
        active_contents = list(
            (
                await self.session.scalars(
                    select(Memory.content).where(Memory.status == "active")
                )
            ).all()
        )
        privacy = await self.get_privacy_config()
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
        privacy = await self.get_privacy_config()
        if not privacy.store_agent_memory:
            return []

        proposals = extract_candidate_proposals(text)
        if not proposals:
            return []

        active_contents = list(
            (
                await self.session.scalars(
                    select(Memory.content).where(Memory.status == "active")
                )
            ).all()
        )

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

            prov = dict(provenance or {})
            if conversation_id:
                prov["conversation_id"] = str(conversation_id)
            if message_id:
                prov["message_id"] = str(message_id)
            prov["origin"] = "agent"

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
        return results

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

        stmt = stmt.order_by(
            desc(MemoryCandidate.created_at), desc(MemoryCandidate.id)
        ).limit(clamped_limit + 1)
        rows = list((await self.session.scalars(stmt)).all())

        has_more = len(rows) > clamped_limit
        items = rows[:clamped_limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        )

        return MemoryCandidatePage(
            items=[_to_candidate_read(r) for r in items],
            next_cursor=next_cursor,
        )

    async def accept_candidate(self, candidate_id: UUID) -> MemoryRead | None:
        """Accept a pending candidate into active owner memory.

        Args:
            candidate_id: UUID of the candidate.

        Returns:
            Created MemoryRead, or None if candidate not found.
        """
        cand = await self.session.get(MemoryCandidate, candidate_id)
        if cand is None or cand.status != "pending":
            return None

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
        return _to_memory_read(mem)

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
        cand = await self.session.get(MemoryCandidate, candidate_id)
        if cand is None:
            return None

        now = datetime.now(UTC)
        cand.status = "rejected"
        cand.rejection_reason = reason
        cand.updated_at = now

        await self.session.commit()
        await self.session.refresh(cand)
        return _to_candidate_read(cand)

    async def purge_memories(self, options: MemoryPurgeRequest) -> MemoryPurgeResponse:
        """Perform durable cleanup of forgotten memories, rejected candidates, or conversation history.

        Args:
            options: MemoryPurgeRequest flags.

        Returns:
            MemoryPurgeResponse with counts of deleted records.
        """
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
            from modules.chat.models import Conversation

            stmt = delete(Conversation).where(Conversation.pinned.is_(False))
            res = await self.session.execute(stmt)
            purged_conversations = res.rowcount or 0

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
        privacy = await self.get_privacy_config()
        # If agent memory is explicitly disabled, do not inject memories into prompt
        if not privacy.store_agent_memory:
            return []

        stmt = (
            select(Memory)
            .where(Memory.status == "active")
            .order_by(desc(Memory.confidence), desc(Memory.created_at))
            .limit(min(limit, 50))
        )
        rows = list((await self.session.scalars(stmt)).all())
        return [_to_memory_read(r) for r in rows]
