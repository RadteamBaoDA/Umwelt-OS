"""MemoryService: memory lifecycle, candidate evaluation and privacy settings."""

import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import delete, desc, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, UnavailableError
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import AccessFence, Scope
from modules.memory._common import (
    _DOCUMENT_MEMORY_CLEANUP_REASON,
    _admit,
    _cache_key,
    _privacy_select,
    bind_page_cursor,
    page_cursor_binding,
    unbind_page_cursor,
)
from modules.memory._privacy_cleanup import _lock_live_provenance_evidence, lock_export_privacy
from modules.memory._reads import (
    _list_filters,
    _to_candidate_read,
    _verified_candidate_read,
    _verified_memory_read,
    _visible_kind_counts,
)
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.memory.schemas import (
    MemoryCandidatePage,
    MemoryCandidateRead,
    MemoryCreate,
    MemoryPage,
    MemoryPrivacyConfig,
    MemoryPrivacyUpdate,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRead,
    MemorySupersedeRequest,
    MemoryUpdate,
)
from modules.memory.selection import evaluate_candidate, extract_candidate_proposals

logger = logging.getLogger(__name__)


def _build_replacement_memory(
    old_item: Memory, payload: MemorySupersedeRequest, now: datetime, *, scope: Scope
) -> Memory:
    """Build the unsaved active replacement for a memory being superseded.

    A derived predecessor (``is_manual is False``) keeps its exact provenance and candidate lineage
    so later source revocation still finds the copy; only an explicitly manual predecessor yields
    an independent manual replacement that merely records ``supersedes``. Pure; the caller adds
    and flushes the row.
    """
    derived_replacement = old_item.is_manual is False
    replacement_provenance = (
        (
            dict(old_item.provenance)
            if isinstance(old_item.provenance, dict)
            else old_item.provenance
        )
        if derived_replacement
        else {"supersedes": str(old_item.id), "origin": "manual"}
    )
    new_item = Memory(
        workspace_id=scope.workspace_id,
        actor_user_id=_actor(scope),
        content=payload.new_content.strip(),
        memory_type=payload.type or old_item.memory_type,
        # The caller sets superseded_by_id, which records supersession. Derived copies must retain their
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
    return new_item


class MemoryService:
    """Service managing memory lifecycle, candidates, evaluation, and privacy settings.

    Construction only binds a session and optional cache client. Every content method requires
    an explicit ``scope`` and the actual ``multi_workspace_enabled`` flag, admits the owner scope
    before any query, and filters by workspace before ordering and LIMIT.
    """

    def __init__(self, session: AsyncSession, redis: Redis | None = None) -> None:
        """Bind active database session and optional cache client.

        Args:
            session: Active asynchronous SQLAlchemy database session.
            redis: Optional Redis client for cache eviction.
        """
        self.session = session
        self.redis = redis

    async def _invalidate_cache(self, scope: Scope) -> None:
        """Evict the workspace's active memory cache key if a Redis client is available."""
        if self.redis is not None:
            try:
                await self.redis.delete(_cache_key(scope))
            except Exception as exc:  # noqa: BLE001  # cache eviction failure must not fail the memory write; logged
                logger.warning("Failed to evict memory cache: %s", exc)

    async def get_memories(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        memory_type: str | None = None,
        status: str = "active",
        query: str | None = None,
        scope: Scope,
        multi_workspace_enabled: bool,
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
            MemoryPage with items, next_cursor and (unfiltered first page only) per-kind counts.
        """
        await _admit(self.session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        clamped_limit = max(1, min(limit, 100))
        filters = _list_filters(status, query, scope=scope)
        stmt = select(Memory).where(*filters)
        binding = page_cursor_binding(
            scope,
            kind="memories",
            status=status,
            memory_type=memory_type,
            query=query,
        )

        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)

        if cursor:
            created_at, identifier = decode_cursor(unbind_page_cursor(cursor, binding))
            stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < (created_at, identifier))

        await lock_export_privacy(self.session, scope.workspace_id, shared=True)
        kind_counts: dict[str, int] | None = None
        counts_capped = False
        # Counts are per kind: only unfiltered first pages carry them. A search skips them so each debounced
        # keystroke does not re-run up to COUNT_SCAN_CAP verified reads while holding the global privacy lock.
        if cursor is None and memory_type is None and not query:
            kind_counts = await _visible_kind_counts(
                self.session,
                filters,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            counts_capped = kind_counts is None
        rows = list(
            (
                await self.session.scalars(
                    stmt.order_by(desc(Memory.created_at), desc(Memory.id)).limit(101)
                )
            ).all()
        )
        items: list[MemoryRead] = []
        examined = 0
        for row in rows[:100]:
            examined += 1
            projected = await _verified_memory_read(
                self.session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if projected is not None:
                items.append(projected)
                if len(items) == clamped_limit:
                    break
        has_more = examined < len(rows) or len(rows) > 100
        next_cursor = (
            bind_page_cursor(
                encode_cursor(rows[examined - 1].created_at, rows[examined - 1].id), binding
            )
            if has_more and examined
            else None
        )

        return MemoryPage(
            items=items,
            next_cursor=next_cursor,
            kind_counts=kind_counts,
            counts_capped=counts_capped,
            total_count=None if kind_counts is None else sum(kind_counts.values()),
        )

    async def get_memory(
        self,
        memory_id: UUID,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead | None:
        """Fetch a single memory item by identifier.

        Args:
            memory_id: UUID of the target memory.

        Returns:
            MemoryRead if found, None otherwise.
        """
        await _admit(self.session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await lock_export_privacy(self.session, scope.workspace_id, shared=True)
        item = await self.session.scalar(
            select(Memory).where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
        )
        return (
            await _verified_memory_read(
                self.session, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if item is not None
            else None
        )

    async def create_memory(
        self,
        payload: MemoryCreate,
        *,
        is_manual: bool = True,
        candidate_id: UUID | None = None,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead:
        """Explicitly create an owner memory or persist an accepted candidate.

        Args:
            payload: Validated MemoryCreate schema.
            is_manual: True if explicitly created by user, False if model-derived.
            candidate_id: Optional reference to the origin MemoryCandidate.

        Returns:
            Newly created MemoryRead.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            payload.provenance,
            require_copy_evidence=not is_manual,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        now = datetime.now(UTC)
        prov = dict(payload.provenance)
        if is_manual:
            prov["origin"] = "manual"

        item = Memory(
            workspace_id=scope.workspace_id,
            actor_user_id=_actor(scope),
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
        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(item)
        await self._invalidate_cache(scope)
        projected = await _verified_memory_read(
            self.session, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if projected is None:
            raise ConflictError("Memory evidence is removed or unavailable")
        return projected

    async def update_memory(
        self,
        memory_id: UUID,
        payload: MemoryUpdate,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead | None:
        """Update an existing active memory's content, type, or reason.

        Args:
            memory_id: Target memory identifier.
            payload: Validated MemoryUpdate schema.

        Returns:
            Updated MemoryRead, or None if not found or not active.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hint = await self.session.execute(
            select(Memory.id, Memory.provenance).where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
        )
        hinted = hint.one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=bool(
                await self.session.scalar(
                    select(Memory.is_manual).where(
                        Memory.id == memory_id,
                        Memory.workspace_id == scope.workspace_id,
                    )
                )
                is False
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        item = await self.session.scalar(
            select(Memory)
            .where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None or item.status != "active":
            return None
        if item.provenance != hinted.provenance:
            raise ConflictError("Memory evidence changed while it was being updated")

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

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(item)
        await self._invalidate_cache(scope)
        return await _verified_memory_read(
            self.session, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )

    async def forget_memory(
        self,
        memory_id: UUID,
        *,
        reason: str | None = None,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead | None:
        """Immediately mark a memory forgotten, removing it from retrieval and purging candidate links.

        Makes content unavailable to retrieval immediately. Durable deletion cleans
        candidate references and evicts memory caches.

        Args:
            memory_id: UUID of the memory to forget.
            reason: Optional explanation for forgetting.

        Returns:
            Forgotten MemoryRead, or None if not found.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hinted = (
            await self.session.execute(
                select(Memory.provenance, Memory.is_manual).where(
                    Memory.id == memory_id,
                    Memory.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=hinted.is_manual is False,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        item = await self.session.scalar(
            select(Memory)
            .where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            return None
        if item.provenance != hinted.provenance:
            raise ConflictError("Memory evidence changed while it was being forgotten")

        now = datetime.now(UTC)
        item.status = "forgotten"
        item.forgotten_at = now
        item.updated_at = now
        if reason:
            item.reason = reason

        # Purge linked candidate payload containing evidence
        if item.candidate_id is not None:
            cand = await self.session.scalar(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.id == item.candidate_id,
                    MemoryCandidate.workspace_id == scope.workspace_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if cand is not None:
                cand.status = "superseded"
                cand.rejection_reason = "Parent memory forgotten by owner"
                cand.content = ""
                cand.reason = _DOCUMENT_MEMORY_CLEANUP_REASON
                cand.provenance = {}

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(item)
        await self._invalidate_cache(scope)
        projected = await _verified_memory_read(
            self.session, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if projected is None:
            raise ConflictError("Memory evidence is removed or unavailable")
        return projected

    async def invalidate_memory(
        self,
        memory_id: UUID,
        *,
        reason: str,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead | None:
        """Mark an active memory invalidated due to factual inaccuracy or policy.

        Args:
            memory_id: Target memory identifier.
            reason: Required rationale explaining why memory is invalid.

        Returns:
            Invalidated MemoryRead, or None if not found.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hinted = (
            await self.session.execute(
                select(Memory.provenance, Memory.is_manual).where(
                    Memory.id == memory_id,
                    Memory.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=hinted.is_manual is False,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        item = await self.session.scalar(
            select(Memory)
            .where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            return None
        if item.provenance != hinted.provenance:
            raise ConflictError("Memory evidence changed while it was being invalidated")

        now = datetime.now(UTC)
        item.status = "invalidated"
        item.invalidated_at = now
        item.updated_at = now
        item.reason = reason

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(item)
        await self._invalidate_cache(scope)
        projected = await _verified_memory_read(
            self.session, item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if projected is None:
            raise ConflictError("Memory evidence is removed or unavailable")
        return projected

    async def supersede_memory(
        self,
        memory_id: UUID,
        payload: MemorySupersedeRequest,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
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
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hinted = (
            await self.session.execute(
                select(Memory.provenance, Memory.is_manual).where(
                    Memory.id == memory_id,
                    Memory.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=hinted.is_manual is False,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        old_item = await self.session.scalar(
            select(Memory)
            .where(
                Memory.id == memory_id,
                Memory.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if old_item is None:
            return None
        if old_item.provenance != hinted.provenance:
            raise ConflictError("Memory evidence changed while it was being superseded")

        now = datetime.now(UTC)
        new_item = _build_replacement_memory(old_item, payload, now, scope=scope)
        self.session.add(new_item)
        await self.session.flush()

        old_item.status = "superseded"
        old_item.superseded_by_id = new_item.id
        old_item.updated_at = now

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(old_item)
        await self.session.refresh(new_item)
        await self._invalidate_cache(scope)

        old_read = await _verified_memory_read(
            self.session, old_item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        new_read = await _verified_memory_read(
            self.session, new_item, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if old_read is None or new_read is None:
            raise ConflictError("Memory evidence is removed or unavailable")
        return old_read, new_read

    async def get_privacy_config(
        self,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryPrivacyConfig:
        """Read owner memory and conversation privacy settings, initializing defaults if needed.

        Returns:
            MemoryPrivacyConfig with store_conversation_history, store_agent_memory, auto_accept_memory.
        """
        await _admit(self.session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        rec = await self.session.scalar(_privacy_select(scope))
        fence: AccessFence | None = None
        if rec is None:
            # Some read routes create these defaults, so fence the insert just like
            # a user write before it becomes part of the snapshot boundary.
            fence = await _admit(
                self.session,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                lock=True,
            )
            # Serialize first-default insertion with consent updates and Chat's absent-row fence.
            await lock_export_privacy(self.session, scope.workspace_id)
            rec = await self.session.scalar(
                _privacy_select(scope).with_for_update().execution_options(populate_existing=True)
            )
        if rec is None:
            # only the locked branch reaches an insert
            if fence is None:
                raise RuntimeError("invariant violated: fence must not be None")
            rec = MemoryPrivacyRecord(
                workspace_id=scope.workspace_id,
                owner_id=_actor(scope),
                store_conversation_history=True,
                store_agent_memory=False,
                auto_accept_memory=False,
            )
            self.session.add(rec)
            await commit_with_replay(
                self.session,
                (),
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            )
            await self.session.refresh(rec)

        return MemoryPrivacyConfig(
            store_conversation_history=rec.store_conversation_history,
            store_agent_memory=rec.store_agent_memory,
            auto_accept_memory=rec.auto_accept_memory,
        )

    async def _read_privacy_config_under_fence(
        self,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
        lock_fence: bool = False,
    ) -> tuple[MemoryPrivacyConfig, AccessFence]:
        """Read fresh consent while holding the privacy lock through the caller's decision.

        Admission precedes the privacy lock; ``lock_fence=True`` returns a locked fence that a
        writing caller later passes to its fenced commit. The returned fence is always the one
        taken after any default-row initialization, which releases earlier locks.

        The default-row initializer admits writes before acquiring this owner lock and may
        commit internally. If the fresh locked read finds no row, release its read transaction,
        initialize through that existing admission path, then reacquire the privacy fence and
        reread with populate_existing before the caller acts on consent.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=lock_fence,
        )
        await lock_export_privacy(self.session, scope.workspace_id)
        rec = await self.session.scalar(
            _privacy_select(scope).with_for_update().execution_options(populate_existing=True)
        )
        if rec is None:
            # Release the privacy owner lock before recreating the default under fresh fences.
            await self.session.rollback()
            await self.get_privacy_config(
                scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            fence = await _admit(
                self.session,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                lock=lock_fence,
            )
            await lock_export_privacy(self.session, scope.workspace_id)
            rec = await self.session.scalar(
                _privacy_select(scope).with_for_update().execution_options(populate_existing=True)
            )
        if rec is None:
            raise UnavailableError("Memory privacy settings are unavailable")
        return MemoryPrivacyConfig(
            store_conversation_history=rec.store_conversation_history,
            store_agent_memory=rec.store_agent_memory,
            auto_accept_memory=rec.auto_accept_memory,
        ), fence

    async def update_privacy_config(
        self,
        payload: MemoryPrivacyUpdate,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryPrivacyConfig:
        """Update owner memory privacy controls.

        Args:
            payload: Validated MemoryPrivacyUpdate fields.

        Returns:
            Updated MemoryPrivacyConfig.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        # Consent changes share the same xact lock Chat holds through admission/publication.
        await lock_export_privacy(self.session, scope.workspace_id)
        rec = await self.session.scalar(
            _privacy_select(scope).with_for_update().execution_options(populate_existing=True)
        )
        if rec is None:
            rec = MemoryPrivacyRecord(workspace_id=scope.workspace_id, owner_id=_actor(scope))
            self.session.add(rec)

        if payload.store_conversation_history is not None:
            rec.store_conversation_history = payload.store_conversation_history
        if payload.store_agent_memory is not None:
            rec.store_agent_memory = payload.store_agent_memory
        if payload.auto_accept_memory is not None:
            rec.auto_accept_memory = payload.auto_accept_memory
        rec.updated_at = datetime.now(UTC)

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
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
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> dict[str, Any]:
        """Evaluate content novelty and usefulness against active memories.

        Args:
            content: Proposed memory text.
            memory_type: Type of memory.

        Returns:
            Dictionary containing novelty_score, usefulness_score, confidence, and recommendation.
        """
        privacy, _ = await self._read_privacy_config_under_fence(
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        source_rows = list(
            (
                await self.session.scalars(
                    select(Memory)
                    .where(Memory.workspace_id == scope.workspace_id, Memory.status == "active")
                    .order_by(desc(Memory.confidence), desc(Memory.created_at))
                    .limit(100)
                )
            ).all()
        )
        active_contents: list[str] = []
        for row in source_rows:
            if (
                await _verified_memory_read(
                    self.session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                )
                is not None
            ):
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

    async def _active_contents(self, *, scope: Scope, multi_workspace_enabled: bool) -> list[str]:
        """Return the contents of the workspace's top-100 active Memories that still verify.

        Ordered by confidence then recency before the limit; each row is re-verified against
        live evidence so unverifiable copies never influence duplicate detection. Read-only.
        """
        source_rows = list(
            (
                await self.session.scalars(
                    select(Memory)
                    .where(Memory.workspace_id == scope.workspace_id, Memory.status == "active")
                    .order_by(desc(Memory.confidence), desc(Memory.created_at))
                    .limit(100)
                )
            ).all()
        )
        active_contents: list[str] = []
        for row in source_rows:
            if (
                await _verified_memory_read(
                    self.session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                )
                is not None
            ):
                active_contents.append(row.content)
        return active_contents

    def _stage_candidates(
        self,
        proposals: list[dict[str, Any]],
        prov: dict[str, Any],
        active_contents: list[str],
        auto_accept_memory: bool,
        *,
        scope: Scope,
    ) -> list[tuple[MemoryCandidate, Memory | None]]:
        """Evaluate proposals and build (but do not add) candidates and auto-accepted Memories.

        Obvious duplicates are skipped. Pure apart from reading the clock; the caller adds the
        rows and flushes them in order so candidate IDs exist before the Memory links to them.
        All proposals are evaluated before the first add/flush, so an evaluation error on a
        later proposal leaves nothing flushed (previously earlier proposals were already flushed
        into the still-uncommitted transaction); the SQL on the success path is unchanged.
        """
        now = datetime.now(UTC)
        staged: list[tuple[MemoryCandidate, Memory | None]] = []
        for p in proposals:
            evaluation = evaluate_candidate(
                p["content"],
                p["type"],
                active_contents,
                auto_accept_enabled=auto_accept_memory,
            )

            # Skip obvious duplicates
            if evaluation.is_duplicate:
                continue

            cand = MemoryCandidate(
                workspace_id=scope.workspace_id,
                actor_user_id=_actor(scope),
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
            staged.append((cand, None))
            if evaluation.should_auto_accept:
                staged[-1] = (
                    cand,
                    Memory(
                        workspace_id=scope.workspace_id,
                        actor_user_id=_actor(scope),
                        content=cand.content,
                        memory_type=cand.memory_type,
                        provenance=prov,
                        confidence=cand.confidence,
                        reason=cand.reason,
                        status="active",
                        is_manual=False,
                        candidate_id=None,
                        created_at=now,
                        updated_at=now,
                    ),
                )
        return staged

    async def suggest_candidates(
        self,
        text: str,
        *,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        provenance: dict[str, Any] | None = None,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> list[MemoryCandidateRead]:
        """Extract memory candidate proposals from text, evaluate them, and persist candidates.

        If store_agent_memory is disabled in privacy settings, candidate suggestions are skipped.
        If auto_accept_memory is enabled, passing candidates are immediately converted into active memories.
        Order: privacy read with the fence locked, live provenance evidence lock, active-memory
        read, candidate inserts, one commit with replay, cache eviction, then a verified re-read
        of every created candidate (unverifiable ones are dropped from the result).

        Args:
            text: Utterance or conversation content to scan.
            conversation_id: Origin conversation UUID.
            message_id: Origin message UUID.
            provenance: Additional provenance attributes.

        Returns:
            List of created MemoryCandidateRead objects.
        """
        privacy, fence = await self._read_privacy_config_under_fence(
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock_fence=True,
        )
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
            self.session,
            prov,
            require_copy_evidence=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )

        active_contents = await self._active_contents(
            scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        staged = self._stage_candidates(
            proposals,
            prov,
            active_contents,
            privacy.auto_accept_memory,
            scope=scope,
        )
        results: list[MemoryCandidateRead] = []
        for cand, mem in staged:
            self.session.add(cand)
            await self.session.flush()

            # If auto-accepted, create active Memory immediately
            if mem is not None:
                mem.candidate_id = cand.id
                self.session.add(mem)

            results.append(_to_candidate_read(cand))

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self._invalidate_cache(scope)
        return await self._verified_candidate_reads(
            results, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )

    async def _verified_candidate_reads(
        self,
        created: list[MemoryCandidateRead],
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> list[MemoryCandidateRead]:
        """Re-read each created candidate and keep only those whose provenance still verifies.

        Runs after the creating commit; candidates that vanished or whose evidence can no longer be
        verified are silently dropped from the returned list (they remain stored). Read-only.
        """
        safe_results: list[MemoryCandidateRead] = []
        for result in created:
            candidate_row = await self.session.scalar(
                select(MemoryCandidate).where(
                    MemoryCandidate.id == result.id,
                    MemoryCandidate.workspace_id == scope.workspace_id,
                )
            )
            if candidate_row is not None:
                projected = await _verified_candidate_read(
                    self.session,
                    candidate_row,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                if projected is not None:
                    safe_results.append(projected)
        return safe_results

    async def get_candidates(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: str = "pending",
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryCandidatePage:
        """List cursor-paginated memory candidates.

        Args:
            limit: Maximum candidates to return.
            cursor: Pagination cursor.
            status: Status filter ('pending', 'accepted', 'rejected').

        Returns:
            MemoryCandidatePage.
        """
        await _admit(self.session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        clamped_limit = max(1, min(limit, 100))
        stmt = select(MemoryCandidate).where(
            MemoryCandidate.workspace_id == scope.workspace_id,
            MemoryCandidate.status == status,
        )

        binding = page_cursor_binding(scope, kind="candidates", status=status)
        if cursor:
            created_at, identifier = decode_cursor(unbind_page_cursor(cursor, binding))
            stmt = stmt.where(
                tuple_(MemoryCandidate.created_at, MemoryCandidate.id) < (created_at, identifier)
            )

        await lock_export_privacy(self.session, scope.workspace_id, shared=True)
        stmt = stmt.order_by(desc(MemoryCandidate.created_at), desc(MemoryCandidate.id)).limit(101)
        rows = list((await self.session.scalars(stmt)).all())
        items: list[MemoryCandidateRead] = []
        examined = 0
        for row in rows[:100]:
            examined += 1
            projected = await _verified_candidate_read(
                self.session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if projected is not None:
                items.append(projected)
                if len(items) == clamped_limit:
                    break
        has_more = examined < len(rows) or len(rows) > 100
        next_cursor = (
            bind_page_cursor(
                encode_cursor(rows[examined - 1].created_at, rows[examined - 1].id), binding
            )
            if has_more and examined
            else None
        )

        return MemoryCandidatePage(
            items=items,
            next_cursor=next_cursor,
        )

    async def accept_candidate(
        self,
        candidate_id: UUID,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryRead | None:
        """Accept a pending candidate into active owner memory.

        Args:
            candidate_id: UUID of the candidate.

        Returns:
            Created MemoryRead, or None if candidate not found.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hinted = (
            await self.session.execute(
                select(MemoryCandidate.provenance, MemoryCandidate.status).where(
                    MemoryCandidate.id == candidate_id,
                    MemoryCandidate.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        cand = await self.session.scalar(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.id == candidate_id,
                MemoryCandidate.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if cand is None or cand.status != "pending":
            return None
        if cand.provenance != hinted.provenance:
            raise ConflictError("Candidate evidence changed while it was being accepted")
        if (
            await _verified_candidate_read(
                self.session, cand, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            is None
        ):
            raise ConflictError("Candidate evidence is removed or unavailable")

        now = datetime.now(UTC)
        cand.status = "accepted"
        cand.updated_at = now

        mem = Memory(
            workspace_id=scope.workspace_id,
            actor_user_id=_actor(scope),
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
        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(mem)
        await self._invalidate_cache(scope)
        projected = await _verified_memory_read(
            self.session, mem, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if projected is None:
            raise ConflictError("Candidate evidence is removed or unavailable")
        return projected

    async def reject_candidate(
        self,
        candidate_id: UUID,
        *,
        reason: str | None = None,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryCandidateRead | None:
        """Reject a pending memory candidate.

        Args:
            candidate_id: UUID of candidate to reject.
            reason: Optional explanation.

        Returns:
            Updated MemoryCandidateRead, or None if not found.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        hinted = (
            await self.session.execute(
                select(MemoryCandidate.provenance).where(
                    MemoryCandidate.id == candidate_id,
                    MemoryCandidate.workspace_id == scope.workspace_id,
                )
            )
        ).one_or_none()
        if hinted is None:
            return None
        await lock_export_privacy(self.session, scope.workspace_id)
        await _lock_live_provenance_evidence(
            self.session,
            hinted.provenance,
            require_copy_evidence=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        cand = await self.session.scalar(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.id == candidate_id,
                MemoryCandidate.workspace_id == scope.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if cand is None:
            return None
        if cand.provenance != hinted.provenance:
            raise ConflictError("Candidate evidence changed while it was being rejected")

        now = datetime.now(UTC)
        cand.status = "rejected"
        cand.rejection_reason = reason
        cand.updated_at = now

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self.session.refresh(cand)
        projected = await _verified_candidate_read(
            self.session, cand, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if projected is None:
            raise ConflictError("Candidate evidence is removed or unavailable")
        return projected

    async def purge_memories(
        self,
        options: MemoryPurgeRequest,
        *,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> MemoryPurgeResponse:
        """Perform durable cleanup of forgotten memories, rejected candidates, or conversation history.

        Args:
            options: MemoryPurgeRequest flags.

        Returns:
            MemoryPurgeResponse with counts of deleted records.
        """
        fence = await _admit(
            self.session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            lock=True,
        )
        # Establish the global privacy-before-Memory-row lock order for this transaction.
        await lock_export_privacy(self.session, scope.workspace_id)

        purged_memories = 0
        purged_candidates = 0
        purged_conversations = 0

        if options.purge_forgotten_memories:
            memory_purge = await self.session.execute(
                delete(Memory).where(
                    Memory.workspace_id == scope.workspace_id,
                    Memory.status == "forgotten",
                )
            )
            purged_memories = cast("CursorResult[Any]", memory_purge).rowcount or 0

        if options.purge_rejected_candidates:
            candidate_purge = await self.session.execute(
                delete(MemoryCandidate).where(
                    MemoryCandidate.workspace_id == scope.workspace_id,
                    MemoryCandidate.status.in_(["rejected", "expired", "superseded"]),
                )
            )
            purged_candidates = cast("CursorResult[Any]", candidate_purge).rowcount or 0

        if options.purge_conversation_history:
            from modules.chat.public import purge_unpinned_conversations

            purged_conversations = await purge_unpinned_conversations(
                self.session,
                owner_id=_actor(scope),
            )

        await commit_with_replay(
            self.session,
            (),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        await self._invalidate_cache(scope)

        return MemoryPurgeResponse(
            purged_memories_count=purged_memories,
            purged_candidates_count=purged_candidates,
            purged_conversations_count=purged_conversations,
        )

    async def get_active_memory_context(
        self,
        *,
        limit: int = 20,
        scope: Scope,
        multi_workspace_enabled: bool,
    ) -> list[MemoryRead]:
        """Fetch active memories formatted for prompt context injection.

        Excluded forgotten or invalidated items. Bounded to specified limit.

        Args:
            limit: Maximum items to return (default 20).

        Returns:
            List of active MemoryRead objects.
        """
        privacy, _ = await self._read_privacy_config_under_fence(
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        # If agent memory is explicitly disabled, do not inject memories into prompt
        if not privacy.store_agent_memory:
            return []

        stmt = (
            select(Memory)
            .where(Memory.workspace_id == scope.workspace_id, Memory.status == "active")
            .order_by(desc(Memory.confidence), desc(Memory.created_at))
            .limit(101)
        )
        rows = list((await self.session.scalars(stmt)).all())
        result: list[MemoryRead] = []
        for row in rows[:100]:
            projected = await _verified_memory_read(
                self.session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            if projected is not None:
                result.append(projected)
                if len(result) >= max(1, min(limit, 50)):
                    break
        return result
