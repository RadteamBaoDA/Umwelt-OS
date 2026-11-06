"""Memory, materialization, brief and raw-document cleanup stage advancers behind ``modules.knowledge.documents.worker``."""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid5

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.events import DomainEvent
from core.realtime import commit_with_replay
from core.storage import get_storage_backend
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents._worker_admit import (
    _admit_cleanup,
    _Attempt,
    _commit,
    _hint,
    _lock_claim,
    _publish_wakeups,
    _receipt_query,
    _settle,
)
from modules.knowledge.documents._worker_progress import (
    _CONTINUATION_DELAY,
    _MATERIALIZATION_PHASES,
    _RAW_URI_UNAVAILABLE,
    _attempt_progress_snapshot,
    _optional_uuid,
    _raw_stage_done,
)
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.memory.public import (
    invalidate_memory_cache,
    lock_export_privacy_in_uow,
    purge_document_copied_evidence_page,
)

if TYPE_CHECKING:
    from modules.automations.public import AutomationCleanupProgress
    from modules.notifications.public import NotificationCleanupProgress

# Keep the original logger name so log routing and caplog filters stay unchanged after the split.
logger = logging.getLogger("modules.knowledge.documents.worker")


async def _advance_memory_cleanup(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    job_scope: InternalJobScope,
    flag: bool,
) -> tuple[bool, bool]:
    """Flush one Memory-owned identity page and update only its bounded receipt state.

    Returns whether payload rows changed and whether the stage reached a terminal result. The
    caller commits this cursor with the page mutations and performs cache eviction afterward.
    """
    if operation.evidence_scope_status != "captured":
        operation.memory_status = "failed"
        operation.memory_error_code = "evidence_identity_unavailable"
        operation.memory_cursor = None
        return False, True
    cursor_state = operation.memory_cursor or {}
    if not isinstance(cursor_state, dict) or set(cursor_state) - {
        "reference_after",
        "owner_cursor",
    }:
        raise ValueError("Stored Memory cleanup cursor is malformed")
    reference_value = cursor_state.get("reference_after")
    owner_cursor = cursor_state.get("owner_cursor")
    if reference_value is not None and not isinstance(reference_value, str):
        raise ValueError("Stored Memory reference cursor is malformed")
    if owner_cursor is not None and not isinstance(owner_cursor, str):
        raise ValueError("Stored Memory owner cursor is malformed")
    reference_after = UUID(reference_value) if reference_value else None
    scope = await documents.list_document_cleanup_evidence_scope(
        session,
        operation.id,
        after=reference_after,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    progress = await purge_document_copied_evidence_page(
        session,
        scope,
        cursor=owner_cursor,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    # A provenance reference outside this bounded page can still be an exact match in a later
    # captured reference page. Keep uncertainty provisional until the final reference page so
    # an early page cannot permanently overcount a record that a later page will scrub.
    if scope.next_cursor is None:
        operation.memory_unresolved_count += progress.unresolved_count
        operation.memory_error_code = progress.unresolved_reason or None
    if progress.changed:
        operation.memory_cache_pending = True
    if not progress.complete:
        if progress.next_cursor is None:
            raise ValueError("Memory cleanup page is incomplete without a continuation cursor")
        operation.memory_status = "running"
        operation.memory_cursor = {
            "reference_after": str(reference_after) if reference_after else None,
            "owner_cursor": progress.next_cursor,
        }
        return progress.changed, False
    if scope.next_cursor is not None:
        operation.memory_status = "running"
        operation.memory_cursor = {
            "reference_after": str(scope.next_cursor),
            "owner_cursor": None,
        }
        return progress.changed, False
    operation.memory_cursor = None
    if operation.memory_unresolved_count:
        operation.memory_status = "failed"
        operation.memory_error_code = "legacy_provenance_unresolved"
    else:
        operation.memory_status = "succeeded"
        operation.memory_error_code = None
    return progress.changed, True


async def _advance_materialization_cleanup(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    job_scope: InternalJobScope,
    flag: bool,
) -> bool:
    """Flush one Notifications/Automations page for this receipt and return whether it is terminal.

    Runs inside the caller's privacy -> receipt lock transaction; owner hooks only lock their own
    rows (stable UUID keyset, <=100) and never the already-deleted Document. The private cursor
    ``{v, phase, reference_after, owner_after}`` walks notifications, then automation triggers, then
    automation runs for each <=100-identity reference page; it is independent of the Chat, Memory
    and Agent cursors. Owner "unavailable" IDs (only reported on the final reference page) add to
    ``materialization_unresolved_count``; that count and the cursor commit together, so a retry
    cannot double count. Unresolved legacy lineage ends ``failed`` and is never relabeled clean.
    """
    if operation.evidence_scope_status != "captured":
        operation.materialization_status = "failed"
        operation.materialization_error_code = "evidence_identity_unavailable"
        operation.materialization_cursor = None
        return True
    state = operation.materialization_cursor or {
        "v": 1,
        "phase": "notifications",
        "reference_after": None,
        "owner_after": None,
    }
    if (
        not isinstance(state, dict)
        or set(state) != {"v", "phase", "reference_after", "owner_after"}
        or state["v"] != 1
        or state["phase"] not in _MATERIALIZATION_PHASES
    ):
        raise ValueError("Stored materialization cleanup cursor is malformed")
    phase = str(state["phase"])
    reference_after = _optional_uuid(state["reference_after"], "materialization reference")
    owner_after = _optional_uuid(state["owner_after"], "materialization owner")
    scope = await documents.list_document_cleanup_evidence_scope(
        session,
        operation.id,
        after=reference_after,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    version_ids = tuple(dict.fromkeys(ref.document_version_id for ref in scope.references))
    final_page = scope.next_cursor is None
    progress: NotificationCleanupProgress | AutomationCleanupProgress
    if phase == "notifications":
        from modules.notifications import public as notifications

        progress = await notifications.scrub_document_evidence(
            session,
            operation_id=operation.id,
            document_id=scope.document_id,
            version_ids=version_ids,
            final_reference_page=final_page,
            after=owner_after,
            limit=100,
            scope=job_scope,
            multi_workspace_enabled=flag,
        )
    else:
        from modules.automations import public as automations

        hook = (
            automations.scrub_document_triggers
            if phase == "triggers"
            else automations.scrub_document_runs
        )
        progress = await hook(
            session,
            operation_id=operation.id,
            document_id=scope.document_id,
            source_id=scope.source_id,
            version_ids=version_ids,
            final_reference_page=final_page,
            after=owner_after,
            limit=100,
            scope=job_scope,
            multi_workspace_enabled=flag,
        )
    # Only terminal (final-page) unavailable rows are reported; provisional IDs are revisited later.
    operation.materialization_unresolved_count += len(progress.unavailable_ids)
    operation.materialization_status = "running"
    if not progress.complete:
        if progress.next_cursor is None:
            raise ValueError(
                "Materialization cleanup page is incomplete without a continuation cursor"
            )
        operation.materialization_cursor = {
            "v": 1,
            "phase": phase,
            "reference_after": str(reference_after) if reference_after else None,
            "owner_after": str(progress.next_cursor),
        }
        return False
    if phase != "runs":
        operation.materialization_cursor = {
            "v": 1,
            "phase": _MATERIALIZATION_PHASES[_MATERIALIZATION_PHASES.index(phase) + 1],
            "reference_after": str(reference_after) if reference_after else None,
            "owner_after": None,
        }
        return False
    if scope.next_cursor is not None:
        operation.materialization_cursor = {
            "v": 1,
            "phase": "notifications",
            "reference_after": str(scope.next_cursor),
            "owner_after": None,
        }
        return False
    operation.materialization_cursor = None
    if operation.materialization_unresolved_count:
        operation.materialization_status = "failed"
        operation.materialization_error_code = "legacy_provenance_unresolved"
    else:
        operation.materialization_status = "succeeded"
        operation.materialization_error_code = None
    return True


async def _advance_brief_cleanup(
    session: AsyncSession,
    operation: DocumentCleanupOperation,
    *,
    job_scope: InternalJobScope,
    flag: bool,
) -> bool:
    """Flush one Dashboard saved-brief page for this receipt and return whether it is terminal.

    Dashboard keys captured prompt dependencies by the detached ``document_id`` alone, so the
    stage needs neither the evidence reference pages nor a live Document. Phase ``sidecars`` scrubs
    briefs whose captured manifest names the Document; phase ``legacy`` then reports (read-only)
    briefs with prose but no manifest, whose Document dependencies are unknowable. Each legacy
    brief adds one to ``brief_unresolved_count`` and the stage ends ``failed`` with
    ``legacy_coverage_unavailable``: reads already withhold such briefs, but they are never
    reported clean and the terminal code prevents a retry loop. Only legacy briefs generated at or
    after the receipt's recorded earliest-version time count (earlier ones cannot mention the
    Document); an unknown time (historical receipt) counts them all. The cursor is ``{v, phase, after}``.
    """
    state = operation.brief_cursor or {"v": 1, "phase": "sidecars", "after": None}
    if (
        not isinstance(state, dict)
        or set(state) != {"v", "phase", "after"}
        or state["v"] != 1
        or state["phase"] not in {"sidecars", "legacy"}
    ):
        raise ValueError("Stored brief cleanup cursor is malformed")
    phase = str(state["phase"])
    after = _optional_uuid(state["after"], "brief")
    from modules.dashboard import public as dashboard

    operation.brief_status = "running"
    if phase == "sidecars":
        progress = await dashboard.clean_document_brief_evidence(
            session,
            operation.document_id,
            after_brief_id=after,
            limit=100,
            scope=job_scope,
            multi_workspace_enabled=flag,
        )
        operation.brief_cursor = {
            "v": 1,
            "phase": "sidecars" if progress.next_cursor else "legacy",
            "after": str(progress.next_cursor) if progress.next_cursor else None,
        }
        return False
    coverage = await dashboard.legacy_brief_coverage(
        session,
        after_brief_id=after,
        limit=100,
        not_before=operation.earliest_version_created_at,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    operation.brief_unresolved_count += len(coverage.candidate_ids)
    if coverage.next_cursor is not None:
        operation.brief_cursor = {"v": 1, "phase": "legacy", "after": str(coverage.next_cursor)}
        return False
    operation.brief_cursor = None
    if operation.brief_unresolved_count:
        operation.brief_status = "failed"
        operation.brief_error_code = "legacy_coverage_unavailable"
    else:
        operation.brief_status = "succeeded"
        operation.brief_error_code = None
    return True


async def _advance_raw_document_cleanup(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    flag: bool,
    event_id: UUID,
) -> bool:
    """Run raw URI cleanup in its own admission -> privacy -> URI -> receipt -> event transaction.

    Returns whether the copied-owner stage may proceed (False: not admitted or claim lost).
    """
    hints: list[documents.SourceCleanupWakeup] = []
    async with factory() as session:
        admitted = await _admit_cleanup(session, event_id, flag)
        if admitted is None:
            await session.rollback()
            return False
        await lock_export_privacy_in_uow(
            session,
            scope=admitted.scope,
            multi_workspace_enabled=flag,
            access_fence=admitted.original,
        )
        hint = await session.scalar(_receipt_query(admitted))
        if hint is None:
            await session.rollback()
            return False
        if _raw_stage_done(hint):
            await session.rollback()
            return True
        raw_uri = hint.raw_uri
        if raw_uri:
            await documents.lock_raw_uri_identity(session, raw_uri)
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if operation is None or operation.raw_uri != raw_uri:
            await session.rollback()
            return False
        if _raw_stage_done(operation):
            await session.rollback()
            return True
        if not await _lock_claim(session, admitted, flag):
            await session.rollback()
            return False
        try:
            if operation.raw_uri is None:
                operation.raw_status = "not_present"
            elif await documents.raw_uri_is_referenced_for_cleanup(
                session,
                operation.id,
                scope=admitted.scope,
                multi_workspace_enabled=flag,
                access_fence=admitted.original,
            ):
                operation.raw_status = "retained_shared"
            else:
                try:
                    # Key validation (ValueError) happens before any unlink inside the backend.
                    await get_storage_backend(settings).delete(operation.raw_uri)
                except ValueError:
                    operation.raw_status = "failed"
                    operation.error_code = _RAW_URI_UNAVAILABLE
                else:
                    operation.raw_status = "succeeded"
            if operation.raw_status != "failed":
                operation.error_code = None
        except (OSError, ValueError, HTTPException, DomainError):
            # Proof or filesystem failure: never unlink on doubt; the stage retries.
            operation.raw_status = "failed"
            operation.error_code = "file_cleanup_failed"
        _hint(hints, operation, "raw")
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)
    return True


async def _evict_memory_cache_after_commit(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    flag: bool,
    attempt: _Attempt,
) -> None:
    """Evict committed Memory state, clear its durable marker under re-admission, and wake Source."""
    admitted = attempt.admitted
    if admitted is None:
        return
    try:
        await invalidate_memory_cache(redis, scope=admitted.scope)
    except Exception as exc:  # noqa: BLE001  # cache eviction is best-effort; cleanup is retried later
        logger.warning("Memory cleanup cache eviction deferred (%s)", type(exc).__name__)
        return
    hints: list[documents.SourceCleanupWakeup] = []
    async with factory() as session:
        if await _admit_cleanup(session, admitted.event_id, flag, status="pending") != admitted:
            await session.rollback()
            return
        await lock_export_privacy_in_uow(
            session,
            scope=admitted.scope,
            multi_workspace_enabled=flag,
            access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if (
            operation is None
            or not operation.memory_cache_pending
            or _attempt_progress_snapshot(operation) != attempt.post_progress
        ):
            await session.rollback()
            return
        operation.memory_cache_pending = False
        # Source's aggregate must observe the independent cache obligation clearing,
        # even when the Memory stage itself had already reached a terminal status.
        _hint(hints, operation, "memory-cache")
        # Never deliver here: the aggregate was computed while the cache was pending, so the
        # main path must re-settle status/copied_status before the event can complete.
        if not await _settle(
            session,
            admitted,
            "pending",
            flag,
            next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY,
            expected_status="pending",
        ):
            await session.rollback()
            return
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)


async def _reopen_cleanup_event(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    operation_id: UUID,
    now: datetime,
) -> bool:
    """Publish or reopen one receipt's deterministic cleanup event in its own admitted session.

    Legacy NULL-epoch or stale receipts resolve to None and are skipped (never re-armed); a
    same-id event of any other shape cannot be adopted (IntegrityError -> skip).
    """
    event_id = uuid5(operation_id, "document-cleanup-requested")
    async with factory() as session:
        try:
            identity = await documents.resolve_document_cleanup_job_identity(
                session,
                operation_id,
                multi_workspace_enabled=flag,
            )
            if identity is None:
                await session.rollback()
                return False
            scope = InternalJobScope(
                workspace_id=identity.workspace_id,
                actor_user_id=identity.actor_user_id,
                membership_revision=identity.membership_revision,
                source_id=identity.source_id,
                source_generation=identity.source_generation,
            )
            fence = AccessFence(
                identity.workspace_id,
                identity.actor_user_id,
                identity.membership_revision,
                identity.configuration_revision,
            )
            event = await ingestion.get_document_cleanup_event(
                session,
                event_id,
                operation_id=operation_id,
                scope=scope,
                multi_workspace_enabled=flag,
            )
            if event is None:
                await ingestion.publish_event(
                    session,
                    DomainEvent(
                        id=event_id,
                        type="document.cleanup.requested",
                        version=1,
                        occurred_at=now,
                        producer="modules.knowledge.documents",
                        payload={"operation_id": str(operation_id)},
                    ),
                    scope=scope,
                    multi_workspace_enabled=flag,
                )
            elif event.status in {"delivered", "failed"}:
                if not await ingestion.settle_document_cleanup_event_in_uow(
                    session,
                    event_id,
                    "pending",
                    operation_id=operation_id,
                    dispatched_at=event.dispatched_at,
                    next_attempt_at=now,
                    expected_status=cast(Literal["delivered", "failed"], event.status),
                    scope=scope,
                    multi_workspace_enabled=flag,
                ):
                    await session.rollback()
                    return False
            else:
                await session.rollback()
                return False
            await commit_with_replay(
                session,
                (),
                scope=scope,
                multi_workspace_enabled=flag,
                access_fence=fence,
            )
            return True
        except (HTTPException, DomainError) as exc:
            await session.rollback()
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            return False
        except IntegrityError:
            await session.rollback()
            return False


async def _reopen_page(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    operation_ids: tuple[UUID, ...],
) -> int:
    """Reopen each listed cleanup event sequentially; return how many were reopened."""
    now = datetime.now(UTC)
    return sum(
        [
            await _reopen_cleanup_event(factory, flag, operation_id, now)
            for operation_id in operation_ids
        ]
    )
