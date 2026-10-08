"""Documents-owned bounded cleanup consumer for raw files and copied-evidence owner stages."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid5

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.events import DomainEvent
from core.realtime import commit_with_replay
from core.storage import storage_path
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.knowledge.documents.schemas import DocumentCleanupJobIdentity
from modules.memory.public import (
    invalidate_memory_cache,
    lock_export_privacy_in_uow,
    purge_document_copied_evidence_page,
)
from modules.sources import public as sources

if TYPE_CHECKING:
    from modules.automations.public import AutomationCleanupProgress
    from modules.notifications.public import NotificationCleanupProgress

logger = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(seconds=30)
_CONTINUATION_DELAY = timedelta(seconds=1)
_MEMORY_RECONCILE_LIMIT = 100
_agent_reconcile_cursor: UUID | None = None
_copied_stage_reconcile_cursor: UUID | None = None
# Failures with these codes are truthful terminal limitations (unavailable lineage); they never retry.
_COPIED_STAGE_TERMINAL_CODES = frozenset({
    "evidence_identity_unavailable", "legacy_provenance_unresolved", "legacy_coverage_unavailable",
})
_MATERIALIZATION_PHASES = ("notifications", "triggers", "runs")


def _source_cleanup_progress_key(operation: DocumentCleanupOperation, stage: str) -> str:
    """Build a bounded deterministic Source event key from non-sensitive child-stage state."""
    state = (
        operation.raw_status,
        operation.error_code,
        operation.evidence_scope_status,
        operation.copied_status,
        operation.copied_error_code,
        operation.chat_status,
        operation.chat_error_code,
        operation.memory_status,
        operation.memory_error_code,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
        operation.materialization_status,
        operation.materialization_error_code,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        operation.brief_unresolved_count,
    )
    fingerprint = hashlib.sha256(
        json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode(),
    ).hexdigest()[:32]
    return f"document-cleanup:{stage}:{fingerprint}"


def _attempt_progress_snapshot(operation: DocumentCleanupOperation) -> tuple[object, ...]:
    """Capture the receipt fields recovery compares before writing stage errors.

    Callers may pass an unlocked read (Agent preflight) or the locked receipt; recovery
    re-compares under the receipt lock so a changed receipt is never overwritten.
    """
    return (
        operation.raw_status,
        operation.evidence_scope_status,
        operation.chat_status,
        operation.copied_status,
        dict(operation.copied_cursor) if isinstance(operation.copied_cursor, dict) else operation.copied_cursor,
        operation.memory_status,
        operation.memory_error_code,
        dict(operation.memory_cursor) if isinstance(operation.memory_cursor, dict) else operation.memory_cursor,
        operation.memory_unresolved_count,
        operation.memory_cache_pending,
        operation.agent_status,
        operation.agent_error_code,
        dict(operation.agent_cursor) if isinstance(operation.agent_cursor, dict) else operation.agent_cursor,
        operation.agent_unresolved_count,
        operation.agent_waiting_for_lease,
        operation.materialization_status,
        operation.materialization_error_code,
        dict(operation.materialization_cursor) if isinstance(operation.materialization_cursor, dict) else operation.materialization_cursor,
        operation.materialization_unresolved_count,
        operation.brief_status,
        operation.brief_error_code,
        dict(operation.brief_cursor) if isinstance(operation.brief_cursor, dict) else operation.brief_cursor,
        operation.brief_unresolved_count,
    )


async def _advance_memory_cleanup(
    session: AsyncSession, operation: DocumentCleanupOperation, *, job_scope: InternalJobScope, flag: bool,
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
    if not isinstance(cursor_state, dict) or set(cursor_state) - {"reference_after", "owner_cursor"}:
        raise ValueError("Stored Memory cleanup cursor is malformed")
    reference_value = cursor_state.get("reference_after")
    owner_cursor = cursor_state.get("owner_cursor")
    if reference_value is not None and not isinstance(reference_value, str):
        raise ValueError("Stored Memory reference cursor is malformed")
    if owner_cursor is not None and not isinstance(owner_cursor, str):
        raise ValueError("Stored Memory owner cursor is malformed")
    reference_after = UUID(reference_value) if reference_value else None
    scope = await documents.list_document_cleanup_evidence_scope(
        session, operation.id, after=reference_after, limit=100,
        scope=job_scope, multi_workspace_enabled=flag,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    progress = await purge_document_copied_evidence_page(
        session, scope, cursor=owner_cursor, limit=100, scope=job_scope, multi_workspace_enabled=flag,
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


def _copied_stage_terminal(status: str, error_code: str | None) -> bool:
    """Return whether a materialization/brief stage is finished, including unavailable limits."""
    return status == "succeeded" or (status == "failed" and error_code in _COPIED_STAGE_TERMINAL_CODES)


def _optional_uuid(value: object, label: str) -> UUID | None:
    """Parse one stored cursor component, rejecting anything but a canonical UUID string or null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Stored {label} cursor is malformed")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError(f"Stored {label} cursor is malformed")
    return parsed


async def _advance_materialization_cleanup(
    session: AsyncSession, operation: DocumentCleanupOperation, *, job_scope: InternalJobScope, flag: bool,
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
        "v": 1, "phase": "notifications", "reference_after": None, "owner_after": None,
    }
    if (not isinstance(state, dict) or set(state) != {"v", "phase", "reference_after", "owner_after"}
            or state["v"] != 1 or state["phase"] not in _MATERIALIZATION_PHASES):
        raise ValueError("Stored materialization cleanup cursor is malformed")
    phase = str(state["phase"])
    reference_after = _optional_uuid(state["reference_after"], "materialization reference")
    owner_after = _optional_uuid(state["owner_after"], "materialization owner")
    scope = await documents.list_document_cleanup_evidence_scope(
        session, operation.id, after=reference_after, limit=100,
        scope=job_scope, multi_workspace_enabled=flag,
    )
    if scope is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    version_ids = tuple(dict.fromkeys(ref.document_version_id for ref in scope.references))
    final_page = scope.next_cursor is None
    progress: NotificationCleanupProgress | AutomationCleanupProgress
    if phase == "notifications":
        from modules.notifications import public as notifications

        progress = await notifications.scrub_document_evidence(
            session, operation_id=operation.id, document_id=scope.document_id,
            version_ids=version_ids, final_reference_page=final_page, after=owner_after, limit=100,
            scope=job_scope, multi_workspace_enabled=flag,
        )
    else:
        from modules.automations import public as automations

        hook = automations.scrub_document_triggers if phase == "triggers" else automations.scrub_document_runs
        progress = await hook(
            session, operation_id=operation.id, document_id=scope.document_id,
            source_id=scope.source_id, version_ids=version_ids,
            final_reference_page=final_page, after=owner_after, limit=100,
            scope=job_scope, multi_workspace_enabled=flag,
        )
    # Only terminal (final-page) unavailable rows are reported; provisional IDs are revisited later.
    operation.materialization_unresolved_count += len(progress.unavailable_ids)
    operation.materialization_status = "running"
    if not progress.complete:
        if progress.next_cursor is None:
            raise ValueError("Materialization cleanup page is incomplete without a continuation cursor")
        operation.materialization_cursor = {
            "v": 1, "phase": phase,
            "reference_after": str(reference_after) if reference_after else None,
            "owner_after": str(progress.next_cursor),
        }
        return False
    if phase != "runs":
        operation.materialization_cursor = {
            "v": 1, "phase": _MATERIALIZATION_PHASES[_MATERIALIZATION_PHASES.index(phase) + 1],
            "reference_after": str(reference_after) if reference_after else None, "owner_after": None,
        }
        return False
    if scope.next_cursor is not None:
        operation.materialization_cursor = {
            "v": 1, "phase": "notifications",
            "reference_after": str(scope.next_cursor), "owner_after": None,
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
    session: AsyncSession, operation: DocumentCleanupOperation, *, job_scope: InternalJobScope, flag: bool,
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
    if (not isinstance(state, dict) or set(state) != {"v", "phase", "after"}
            or state["v"] != 1 or state["phase"] not in {"sidecars", "legacy"}):
        raise ValueError("Stored brief cleanup cursor is malformed")
    phase = str(state["phase"])
    after = _optional_uuid(state["after"], "brief")
    from modules.dashboard import public as dashboard

    operation.brief_status = "running"
    if phase == "sidecars":
        progress = await dashboard.clean_document_brief_evidence(
            session, operation.document_id, after_brief_id=after, limit=100,
            scope=job_scope, multi_workspace_enabled=flag,
        )
        operation.brief_cursor = {
            "v": 1, "phase": "sidecars" if progress.next_cursor else "legacy",
            "after": str(progress.next_cursor) if progress.next_cursor else None,
        }
        return False
    coverage = await dashboard.legacy_brief_coverage(
        session, after_brief_id=after, limit=100, not_before=operation.earliest_version_created_at,
        scope=job_scope, multi_workspace_enabled=flag,
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


def _fail_copied_stage(
    operation: DocumentCleanupOperation, stage: str, code: str, *, reset: bool,
) -> None:
    """Record a retryable failure of one materialization/brief stage; a reset also restarts its cursor.

    A cursor reset zeroes the stage's unresolved count because the restart recounts from scratch.
    """
    setattr(operation, f"{stage}_status", "failed")
    setattr(operation, f"{stage}_error_code", code)
    if reset:
        setattr(operation, f"{stage}_cursor", None)
        setattr(operation, f"{stage}_unresolved_count", 0)
    operation.copied_status = "failed"
    operation.copied_error_code = code
    operation.status = "failed"
    operation.error_code = code


@dataclass(frozen=True)
class _Admitted:
    """Detached proof that one claimed cleanup event is admitted under its original authority."""

    identity: DocumentCleanupJobIdentity
    scope: InternalJobScope
    original: AccessFence
    event_id: UUID
    dispatched_at: datetime


@dataclass
class _Attempt:
    """Plain-scalar attempt state read after rollback instead of ORM attributes."""

    admitted: _Admitted | None = None
    progress: tuple[object, ...] | None = None
    stage: str = "unknown"
    hints: list[documents.SourceCleanupWakeup] = field(default_factory=list)
    post_progress: tuple[object, ...] | None = None
    evict: bool = False


def _worker_flag(ctx: dict[str, object]) -> bool:
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    return enabled


async def _admit_cleanup(
    session: AsyncSession, event_id: UUID, flag: bool, *, status: str = "queued",
) -> _Admitted | None:
    """Admit one claimed cleanup event under the receipt's ORIGINAL actor/membership/configuration.

    Order: event scope resolution, claimed (queued, dispatched) outbox row, receipt authority,
    then a fresh fence equal to the captured one. No receipt body or raw URI is read before this
    returns; None means no mutation (missing, stale, legacy NULL-epoch or not claimed).
    """
    try:
        operation_id = await ingestion.read_document_cleanup_event_operation_id(session, event_id)
        if operation_id is None:
            return None
        scope = await ingestion.resolve_ingestion_event_scope(session, event_id, multi_workspace_enabled=flag)
        if scope is None:
            return None
        event = await ingestion.get_document_cleanup_event(
            session, event_id, operation_id=operation_id, scope=scope, multi_workspace_enabled=flag,
        )
        if event is None or event.status != status or event.dispatched_at is None:
            return None
        identity = await documents.read_document_cleanup_job_identity(
            session, operation_id, scope=scope, multi_workspace_enabled=flag,
        )
        if identity is None:
            return None
        original = AccessFence(
            identity.workspace_id, identity.actor_user_id, identity.membership_revision,
            identity.configuration_revision,
        )
        if await read_access_fence(session, scope=scope, multi_workspace_enabled=flag) != original:
            return None
    except HTTPException:
        return None
    return _Admitted(identity, scope, original, event_id, event.dispatched_at)


def _receipt_query(admitted: _Admitted, *, lock: bool = False) -> Select[DocumentCleanupOperation]:
    """Select the admitted receipt with workspace and actor predicates on every statement."""
    identity = admitted.identity
    statement = select(DocumentCleanupOperation).where(
        DocumentCleanupOperation.id == identity.operation_id,
        DocumentCleanupOperation.workspace_id == identity.workspace_id,
        DocumentCleanupOperation.actor_user_id == identity.actor_user_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return statement


async def _settle(
    session: AsyncSession, admitted: _Admitted, status: Literal["failed", "pending", "delivered"], flag: bool,
    *, next_attempt_at: datetime | None = None,
    expected_status: Literal["queued", "pending", "delivered", "failed"] = "queued",
) -> bool:
    """CAS the cleanup event on the claim stamp captured at admission; False means the claim moved."""
    return await ingestion.settle_document_cleanup_event_in_uow(
        session, admitted.event_id, status, operation_id=admitted.identity.operation_id,
        dispatched_at=admitted.dispatched_at, next_attempt_at=next_attempt_at,
        expected_status=expected_status, scope=admitted.scope, multi_workspace_enabled=flag,
    )


async def _commit(session: AsyncSession, admitted: _Admitted, flag: bool) -> None:
    await commit_with_replay(
        session, (), scope=admitted.scope, multi_workspace_enabled=flag, access_fence=admitted.original,
    )


async def _lock_claim(session: AsyncSession, admitted: _Admitted, flag: bool) -> bool:
    """Lock the single cleanup outbox row and require the original queued claim."""
    event = await ingestion.lock_document_cleanup_event_in_uow(
        session, admitted.event_id, operation_id=admitted.identity.operation_id,
        scope=admitted.scope, multi_workspace_enabled=flag,
    )
    return event is not None and event.status == "queued" and event.dispatched_at == admitted.dispatched_at


def _hint(hints: list[documents.SourceCleanupWakeup], operation: DocumentCleanupOperation, stage: str) -> None:
    """Snapshot a content-free Source wakeup hint while the receipt is held; publish only after commit."""
    hints.append(documents.source_cleanup_wakeup_hint(
        operation, progress_key=_source_cleanup_progress_key(operation, stage),
    ))


async def _publish_wakeups(
    factory: async_sessionmaker[AsyncSession], flag: bool, hints: list[documents.SourceCleanupWakeup],
) -> None:
    """Wake each open Source-purge observer from a fresh lock-free session; failures only log.

    The durable fallback is the Sources coverage reconciler, so nothing here may fail a stage.
    """
    for hint in hints:
        try:
            async with factory() as session:
                observers = list(dict.fromkeys(
                    ([hint.linked_operation_id] if hint.linked_operation_id else [])
                    + list(await sources.discover_source_purge_observer_ids(
                        session, workspace_id=hint.workspace_id, source_id=hint.source_id, limit=100,
                    )),
                ))
                await session.rollback()
        except Exception as exc:  # noqa: BLE001  # boundary: durable reconciler is the fallback
            logger.warning("Source cleanup wakeup discovery deferred (%s)", type(exc).__name__)
            continue
        for observer in observers:
            try:
                async with factory() as session:
                    await documents.publish_source_cleanup_wakeup(
                        session, hint, observer, multi_workspace_enabled=flag,
                    )
            except Exception as exc:  # noqa: BLE001  # boundary: durable reconciler is the fallback
                logger.warning("Source cleanup wakeup deferred (%s)", type(exc).__name__)


async def _advance_raw_document_cleanup(
    factory: async_sessionmaker[AsyncSession], settings: Settings, flag: bool, event_id: UUID,
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
            session, scope=admitted.scope, multi_workspace_enabled=flag, access_fence=admitted.original,
        )
        hint = await session.scalar(_receipt_query(admitted))
        if hint is None:
            await session.rollback()
            return False
        if hint.raw_status in {"not_present", "retained_shared", "succeeded"}:
            await session.rollback()
            return True
        raw_uri = hint.raw_uri
        if raw_uri:
            await documents.lock_raw_uri_identity(session, raw_uri)
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if operation is None or operation.raw_uri != raw_uri:
            await session.rollback()
            return False
        if operation.raw_status in {"not_present", "retained_shared", "succeeded"}:
            await session.rollback()
            return True
        if not await _lock_claim(session, admitted, flag):
            await session.rollback()
            return False
        try:
            if operation.raw_uri is None:
                operation.raw_status = "not_present"
            elif await documents.raw_uri_is_referenced_for_cleanup(
                session, operation.id, scope=admitted.scope, multi_workspace_enabled=flag,
                access_fence=admitted.original,
            ):
                operation.raw_status = "retained_shared"
            else:
                storage_path(settings.data_dir, operation.raw_uri).unlink(missing_ok=True)
                operation.raw_status = "succeeded"
            operation.error_code = None
        except (OSError, ValueError, HTTPException):
            # Proof or filesystem failure: never unlink on doubt; the stage retries.
            operation.raw_status = "failed"
            operation.error_code = "file_cleanup_failed"
        _hint(hints, operation, "raw")
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)
    return True


async def _evict_memory_cache_after_commit(
    factory: async_sessionmaker[AsyncSession], redis: Redis, flag: bool, attempt: _Attempt,
) -> None:
    """Evict committed Memory state, clear its durable marker under re-admission, and wake Source."""
    admitted = attempt.admitted
    if admitted is None:
        return
    try:
        await invalidate_memory_cache(redis, scope=admitted.scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory cleanup cache eviction deferred (%s)", type(exc).__name__)
        return
    hints: list[documents.SourceCleanupWakeup] = []
    async with factory() as session:
        if await _admit_cleanup(session, admitted.event_id, flag, status="pending") != admitted:
            await session.rollback()
            return
        await lock_export_privacy_in_uow(
            session, scope=admitted.scope, multi_workspace_enabled=flag, access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if (operation is None or not operation.memory_cache_pending
                or _attempt_progress_snapshot(operation) != attempt.post_progress):
            await session.rollback()
            return
        operation.memory_cache_pending = False
        # Source's aggregate must observe the independent cache obligation clearing,
        # even when the Memory stage itself had already reached a terminal status.
        _hint(hints, operation, "memory-cache")
        # Never deliver here: the aggregate was computed while the cache was pending, so the
        # main path must re-settle status/copied_status before the event can complete.
        if not await _settle(
            session, admitted, "pending", flag,
            next_attempt_at=datetime.now(UTC) + _CONTINUATION_DELAY, expected_status="pending",
        ):
            await session.rollback()
            return
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)


async def _reopen_cleanup_event(
    factory: async_sessionmaker[AsyncSession], flag: bool, operation_id: UUID, now: datetime,
) -> bool:
    """Publish or reopen one receipt's deterministic cleanup event in its own admitted session.

    Legacy NULL-epoch or stale receipts resolve to None and are skipped (never re-armed); a
    same-id event of any other shape cannot be adopted (IntegrityError -> skip).
    """
    event_id = uuid5(operation_id, "document-cleanup-requested")
    async with factory() as session:
        try:
            identity = await documents.resolve_document_cleanup_job_identity(
                session, operation_id, multi_workspace_enabled=flag,
            )
            if identity is None:
                await session.rollback()
                return False
            scope = InternalJobScope(
                workspace_id=identity.workspace_id, actor_user_id=identity.actor_user_id,
                membership_revision=identity.membership_revision, source_id=identity.source_id,
                source_generation=identity.source_generation,
            )
            fence = AccessFence(
                identity.workspace_id, identity.actor_user_id, identity.membership_revision,
                identity.configuration_revision,
            )
            event = await ingestion.get_document_cleanup_event(
                session, event_id, operation_id=operation_id, scope=scope, multi_workspace_enabled=flag,
            )
            if event is None:
                await ingestion.publish_event(session, DomainEvent(
                    id=event_id, type="document.cleanup.requested", version=1, occurred_at=now,
                    producer="modules.knowledge.documents", payload={"operation_id": str(operation_id)},
                ), scope=scope, multi_workspace_enabled=flag)
            elif event.status in {"delivered", "failed"}:
                if not await ingestion.settle_document_cleanup_event_in_uow(
                    session, event_id, "pending", operation_id=operation_id,
                    dispatched_at=event.dispatched_at, next_attempt_at=now,
                    expected_status=cast(Literal["delivered", "failed"], event.status),
                    scope=scope, multi_workspace_enabled=flag,
                ):
                    await session.rollback()
                    return False
            else:
                await session.rollback()
                return False
            await commit_with_replay(
                session, (), scope=scope, multi_workspace_enabled=flag, access_fence=fence,
            )
            return True
        except HTTPException as exc:
            await session.rollback()
            if exc.status_code not in {401, 403, 404, 409}:
                raise
            return False
        except IntegrityError:
            await session.rollback()
            return False


async def _reopen_page(
    factory: async_sessionmaker[AsyncSession], flag: bool, operation_ids: tuple[UUID, ...],
) -> int:
    now = datetime.now(UTC)
    return sum([await _reopen_cleanup_event(factory, flag, operation_id, now) for operation_id in operation_ids])


async def reconcile_document_memory_cleanup(ctx: dict[str, object]) -> int:
    """Requeue at most 100 captured historical receipts whose Chat stage already succeeded.

    Identity-only discovery excludes legacy NULL-epoch receipts; each ID is admitted and
    published in its own session. Like all cleanup work it is not module-gated.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_memory_cleanup_ids(
            session, limit=_MEMORY_RECONCILE_LIMIT,
        )
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def reconcile_document_agent_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal Agent stage."""
    global _agent_reconcile_cursor
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_agent_cleanup_ids(
            session, after=_agent_reconcile_cursor, limit=100,
        )
        if not operation_ids and _agent_reconcile_cursor is not None:
            _agent_reconcile_cursor = None
            operation_ids = await documents.pending_document_agent_cleanup_ids(session, limit=100)
        if operation_ids:
            _agent_reconcile_cursor = operation_ids[-1]
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def reconcile_document_copied_stage_cleanup(ctx: dict[str, object]) -> int:
    """Fairly scan one bounded UUID page of receipts with a nonterminal materialization/brief stage.

    Reopens a delivered or failed deterministic cleanup event idempotently (including events the
    dispatcher quarantined before this envelope was accepted) and preserves the schedule of a
    pending one. Terminal unavailable stages are excluded by the Documents query.
    """
    global _copied_stage_reconcile_cursor
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    flag = _worker_flag(ctx)
    async with factory() as session:
        operation_ids = await documents.pending_document_copied_stage_cleanup_ids(
            session, after=_copied_stage_reconcile_cursor, limit=100,
        )
        if not operation_ids and _copied_stage_reconcile_cursor is not None:
            _copied_stage_reconcile_cursor = None
            operation_ids = await documents.pending_document_copied_stage_cleanup_ids(session, limit=100)
        if operation_ids:
            _copied_stage_reconcile_cursor = operation_ids[-1]
        await session.rollback()
    return await _reopen_page(factory, flag, operation_ids)


async def _requeue(
    factory: async_sessionmaker[AsyncSession], flag: bool, admitted: _Admitted, delay: timedelta,
) -> None:
    """Reschedule the exact original claim from a fresh admission after dropping all locks."""
    async with factory() as session:
        if await _admit_cleanup(session, admitted.event_id, flag) != admitted:
            await session.rollback()
            return
        if await _settle(session, admitted, "pending", flag, next_attempt_at=datetime.now(UTC) + delay):
            await _commit(session, admitted, flag)
        else:
            await session.rollback()


def _cursor_state_agent(cursor: object) -> dict[str, object]:
    state = cursor or {"v": 1, "reference_after": None, "candidate_after": None, "phase": "discover"}
    if not isinstance(state, dict) or set(state) != {
        "v", "reference_after", "candidate_after", "phase",
    } or state.get("v") != 1 or state.get("phase") not in {"discover", "finalize"}:
        raise ValueError("Stored Agent cleanup cursor is malformed")
    return state


async def _advance_copied_cleanup(
    factory: async_sessionmaker[AsyncSession], flag: bool, identifier: UUID, attempt: _Attempt,
) -> bool:
    """Run one privacy -> receipt -> event locked page of every copied-owner stage; True if committed."""
    async with factory() as session:
        admitted = await _admit_cleanup(session, identifier, flag)
        if admitted is None:
            await session.rollback()
            return False
        attempt.admitted = admitted
        job_scope = admitted.scope
        operation_id = admitted.identity.operation_id
        operation_hint = await session.scalar(_receipt_query(admitted))
        if operation_hint is None:
            await session.rollback()
            return False
        # Agent lease preparation must happen before the shared privacy or receipt lock.
        # The same session retains a successful transaction advisory lease through commit.
        agent_evidence = None
        agent_preflight = None
        agent_reference_after = None
        operation_id_snapshot = operation_hint.id
        source_id_snapshot = operation_hint.source_id
        document_id_snapshot = operation_hint.document_id
        evidence_scope_snapshot = operation_hint.evidence_scope_status
        agent_status_snapshot = operation_hint.agent_status
        agent_cursor_snapshot = (
            dict(operation_hint.agent_cursor)
            if isinstance(operation_hint.agent_cursor, dict) else operation_hint.agent_cursor
        )
        if operation_hint.agent_status in {"queued", "running"} and operation_hint.evidence_scope_status == "captured":
            # Preflight failures (bad cursor, lease DB errors) need Agent-stage recovery. The
            # unlocked snapshot lets recovery compare under the receipt lock before writing.
            attempt.stage = "agent"
            attempt.progress = _attempt_progress_snapshot(operation_hint)
            cursor_state = _cursor_state_agent(operation_hint.agent_cursor)
            reference_value = cursor_state.get("reference_after")
            candidate_cursor = cursor_state.get("candidate_after")
            if reference_value is not None and not isinstance(reference_value, str):
                raise ValueError("Stored Agent reference cursor is malformed")
            if candidate_cursor is not None and not isinstance(candidate_cursor, str):
                raise ValueError("Stored Agent candidate cursor is malformed")
            agent_reference_after = UUID(reference_value) if reference_value else None
            agent_evidence = await documents.list_document_cleanup_evidence_scope(
                session, operation_id, after=agent_reference_after, limit=100,
                scope=job_scope, multi_workspace_enabled=flag,
            )
            if agent_evidence is None:
                raise ValueError("Document cleanup evidence scope is unavailable")
            from modules.agents import public as agents

            agent_preflight = await agents.preflight_document_copied_evidence_lease(
                session, agent_evidence, cursor=candidate_cursor, limit=100,
                scope=job_scope, multi_workspace_enabled=flag,
            )
            if agent_preflight.blocked:
                # No privacy or receipt row was locked; schedule retry from the durable cursor.
                if await _settle(session, admitted, "pending", flag,
                                 next_attempt_at=datetime.now(UTC) + _RETRY_DELAY):
                    await _commit(session, admitted, flag)
                else:
                    await session.rollback()
                return False

        # Shared Memory consent lock precedes URI, receipt, Chat and Memory owner locks.
        await lock_export_privacy_in_uow(
            session, scope=job_scope, multi_workspace_enabled=flag, access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if operation is None or not await _lock_claim(session, admitted, flag):
            await session.rollback()
            return False

        attempt.progress = _attempt_progress_snapshot(operation)
        if attempt.stage == "agent":
            attempt.stage = "unknown"  # Preflight and lease checks passed; later stages set their own.

        scope_stale = (
            operation.id != operation_id_snapshot
            or operation.source_id != source_id_snapshot
            or operation.document_id != document_id_snapshot
            or operation.evidence_scope_status != evidence_scope_snapshot
            or operation.agent_status != agent_status_snapshot
            or operation.agent_cursor != agent_cursor_snapshot
        )
        if agent_evidence is not None:
            locked_agent_evidence = await documents.list_document_cleanup_evidence_scope(
                session, operation.id, after=agent_reference_after, limit=100,
                scope=job_scope, multi_workspace_enabled=flag,
            )
            scope_stale = scope_stale or locked_agent_evidence != agent_evidence
        if scope_stale:
            # The preflight is detached, so discard its advisory lease and repeat from the
            # receipt's current cursor before touching owner rows.
            await session.rollback()
            await _requeue(factory, flag, admitted, _CONTINUATION_DELAY)
            return False

        memory_terminal = operation.memory_status == "succeeded" or (
            operation.memory_status == "failed" and operation.memory_error_code in {
                "legacy_provenance_unresolved", "evidence_identity_unavailable",
            }
        )
        agent_terminal = operation.agent_status in {"succeeded", "failed"}
        if (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                and operation.chat_status == "succeeded" and memory_terminal and agent_terminal
                and _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code)
                and _copied_stage_terminal(operation.brief_status, operation.brief_error_code)
                and not operation.memory_cache_pending
                and operation.status in {"succeeded", "failed"}):
            # Duplicate deliveries must not reopen a settled receipt; an unsettled one re-settles below.
            if await _settle(session, admitted, "delivered", flag):
                await _commit(session, admitted, flag)
                attempt.progress = None
                return True
            await session.rollback()
            return False

        operation.status = "running"
        operation.error_code = None
        operation.copied_status = "running"
        next_attempt_at: datetime | None = None
        terminal_scope_failure = False

        if (operation.evidence_scope_status == "captured"
                and operation.agent_status in {"queued", "running"}):
            if agent_evidence is None or agent_preflight is None:
                raise ValueError("Agent cleanup lease preflight is unavailable")
            cursor_state = _cursor_state_agent(operation.agent_cursor)
            reference_value = cursor_state.get("reference_after")
            candidate_cursor = cursor_state.get("candidate_after")
            if not isinstance(reference_value, (str, type(None))) or not isinstance(candidate_cursor, (str, type(None))):
                raise ValueError("Stored Agent cleanup cursor is malformed")
            reference_after = UUID(reference_value) if reference_value else None
            from modules.agents import public as agents

            attempt.stage = "agent"
            agent_progress = await agents.purge_document_copied_evidence_page(
                session, agent_evidence, cursor=candidate_cursor, limit=100,
                preflight=agent_preflight, scope=job_scope, multi_workspace_enabled=flag,
            )
            if agent_progress.preflight_stale:
                # Drop receipt/privacy locks and any successful stale lease before retrying.
                await session.rollback()
                await _requeue(factory, flag, admitted, _CONTINUATION_DELAY)
                return False
            operation.agent_unresolved_count = agent_progress.unavailable_count
            operation.agent_waiting_for_lease = agent_progress.lease_pending
            operation.agent_error_code = None
            if agent_progress.complete:
                if agent_evidence.next_cursor is not None:
                    operation.agent_status = "running"
                    operation.agent_cursor = {
                        "v": 1, "reference_after": str(agent_evidence.next_cursor),
                        "candidate_after": None, "phase": "discover",
                    }
                    next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
                else:
                    operation.agent_cursor = None
                    operation.agent_waiting_for_lease = False
                    if agent_progress.unavailable_count:
                        operation.agent_status = "failed"
                        operation.agent_error_code = "evidence_identity_unavailable"
                    else:
                        operation.agent_status = "succeeded"
                    _hint(attempt.hints, operation, "agent")
            else:
                if agent_progress.next_cursor is None:
                    raise ValueError("Agent cleanup page is incomplete without a continuation cursor")
                operation.agent_status = "running"
                operation.agent_cursor = {
                    "v": 1,
                    "reference_after": str(reference_after) if reference_after else None,
                    "candidate_after": agent_progress.next_cursor,
                    "phase": "finalize" if agent_progress.lease_pending else "discover",
                }
                next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY

        if operation.evidence_scope_status != "captured":
            operation.chat_status = "failed"
            operation.chat_error_code = "evidence_identity_unavailable"
            operation.copied_status = "failed"
            operation.copied_error_code = "evidence_identity_unavailable"
            operation.error_code = "evidence_identity_unavailable"
            operation.agent_status = "failed"
            operation.agent_error_code = "evidence_identity_unavailable"
            operation.agent_cursor = None
            operation.agent_waiting_for_lease = False
            terminal_scope_failure = True
        elif operation.chat_status != "succeeded":
            attempt.stage = "chat"
            operation.chat_status = "running"
            operation.chat_error_code = None
            cursor_state = operation.copied_cursor or {}
            if not isinstance(cursor_state, dict):
                cursor_state = {}
            reference_after = UUID(str(cursor_state["reference_after"])) if cursor_state.get("reference_after") else None
            chat_cursor = cursor_state.get("chat_cursor")
            if chat_cursor is not None and not isinstance(chat_cursor, str):
                raise ValueError("Stored Chat cleanup cursor is malformed")
            evidence = await documents.list_document_cleanup_evidence_scope(
                session, operation.id, after=reference_after, limit=100,
                scope=job_scope, multi_workspace_enabled=flag,
            )
            if evidence is None:
                raise ValueError("Document cleanup evidence scope is unavailable")
            from modules.chat import public as chat

            progress = await chat.purge_document_copied_evidence_page(
                session, evidence, cursor=chat_cursor, limit=100,
                scope=job_scope, multi_workspace_enabled=flag,
            )
            if progress.complete:
                if evidence.next_cursor is None:
                    operation.chat_status = "succeeded"
                    operation.chat_error_code = None
                    operation.copied_cursor = None
                else:
                    operation.copied_cursor = {
                        "reference_after": str(evidence.next_cursor),
                        "chat_cursor": None,
                    }
                    next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY
            else:
                if progress.next_cursor is None:
                    raise ValueError("Chat cleanup page is incomplete without a continuation cursor")
                operation.copied_cursor = {
                    "reference_after": str(reference_after) if reference_after else None,
                    "chat_cursor": progress.next_cursor,
                }
                next_attempt_at = datetime.now(UTC) + _CONTINUATION_DELAY

        if operation.chat_status == "succeeded" and operation.memory_status not in {
            "succeeded",
        } and not (
            operation.memory_status == "failed"
            and operation.memory_error_code in {
                "legacy_provenance_unresolved", "evidence_identity_unavailable",
            }
        ):
            if operation.memory_cache_pending:
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
            else:
                attempt.stage = "memory"
                operation.memory_status = "running"
                operation.memory_error_code = None
                changed, terminal = await _advance_memory_cleanup(
                    session, operation, job_scope=job_scope, flag=flag,
                )
                if changed:
                    attempt.evict = True
                if not terminal:
                    next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY

        # Materialization (Notifications/Automations) and saved-brief stages keep their own
        # cursors and run under the same privacy -> receipt locks; one page each per delivery.
        if not _copied_stage_terminal(operation.materialization_status, operation.materialization_error_code):
            attempt.stage = "materialization"
            operation.materialization_error_code = None
            if not await _advance_materialization_cleanup(session, operation, job_scope=job_scope, flag=flag):
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY
        if not _copied_stage_terminal(operation.brief_status, operation.brief_error_code):
            attempt.stage = "brief"
            operation.brief_error_code = None
            if not await _advance_brief_cleanup(session, operation, job_scope=job_scope, flag=flag):
                next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY

        if operation.raw_status == "failed":
            operation.error_code = operation.error_code or "file_cleanup_failed"
            next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
        if operation.chat_status == "failed" and not terminal_scope_failure:
            operation.copied_status = "failed"
            operation.copied_error_code = operation.chat_error_code or "chat_cleanup_failed"
            operation.error_code = operation.copied_error_code
            next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY

        if operation.agent_status == "running":
            next_attempt_at = next_attempt_at or datetime.now(UTC) + _CONTINUATION_DELAY
        elif operation.agent_status == "failed":
            operation.copied_error_code = operation.agent_error_code or "agent_cleanup_failed"
            operation.error_code = operation.copied_error_code

        for stage in ("materialization", "brief"):
            if getattr(operation, f"{stage}_status") == "failed":
                operation.copied_error_code = getattr(operation, f"{stage}_error_code") or f"{stage}_cleanup_failed"
                operation.error_code = operation.copied_error_code

        local_failed = (
            operation.raw_status == "failed" or operation.chat_status == "failed"
            or operation.memory_status == "failed" or operation.agent_status == "failed"
            or operation.materialization_status == "failed" or operation.brief_status == "failed"
        )
        if local_failed:
            operation.status = "failed"
            operation.copied_status = "failed"
        elif (operation.raw_status in {"not_present", "retained_shared", "succeeded"}
                and operation.chat_status == "succeeded" and operation.memory_status == "succeeded"
                and not operation.memory_cache_pending and operation.agent_status == "succeeded"
                and operation.materialization_status == "succeeded"
                and operation.brief_status == "succeeded"):
            # Aggregate success only when every required owner stage genuinely completed.
            operation.status = "succeeded"
            operation.copied_status = "succeeded"
            operation.copied_error_code = None
            operation.error_code = None
        else:
            operation.status = "running"
            operation.copied_status = "running"

        if (operation.chat_status in {"succeeded", "failed"}
                or operation.memory_status in {"succeeded", "failed"}
                or operation.agent_status in {"succeeded", "failed"}
                or operation.materialization_status in {"succeeded", "failed"}
                or operation.brief_status in {"succeeded", "failed"}):
            # Snapshotted after the aggregate is final so Source observes the settled state.
            _hint(attempt.hints, operation, "aggregate")

        if operation.memory_cache_pending:
            attempt.evict = True
            next_attempt_at = next_attempt_at or datetime.now(UTC) + _RETRY_DELAY
        attempt.post_progress = _attempt_progress_snapshot(operation)

        if next_attempt_at is not None:
            settled = await _settle(session, admitted, "pending", flag, next_attempt_at=next_attempt_at)
        else:
            # The event is complete for Documents and Chat even while other copy owners remain pending.
            settled = await _settle(session, admitted, "delivered", flag)
        if not settled:
            await session.rollback()
            attempt.hints.clear()
            attempt.evict = False
            return False
        await _commit(session, admitted, flag)
    attempt.progress, attempt.stage = None, "unknown"  # Committed: later failures own no stage recovery.
    return True


async def _recover_attempt(
    factory: async_sessionmaker[AsyncSession], flag: bool, identifier: UUID, attempt: _Attempt,
    *, malformed: bool,
) -> None:
    """Write a stage error only when fresh admission and the receipt still equal this attempt's snapshot.

    Everything compared is a plain-scalar snapshot taken before the rollback; a missing snapshot
    means no mutation and the dispatcher's stale-claim recovery reclaims the work.
    """
    snapshot, progress, stage = attempt.admitted, attempt.progress, attempt.stage
    if snapshot is None or progress is None:
        return
    hints: list[documents.SourceCleanupWakeup] = []
    code = "cursor_reset" if malformed else "cleanup_failed"
    async with factory() as session:
        admitted = await _admit_cleanup(session, identifier, flag)
        if admitted is None or admitted != snapshot:
            await session.rollback()
            return
        await lock_export_privacy_in_uow(
            session, scope=admitted.scope, multi_workspace_enabled=flag, access_fence=admitted.original,
        )
        operation = await session.scalar(_receipt_query(admitted, lock=True))
        if (operation is None or _attempt_progress_snapshot(operation) != progress
                or not await _lock_claim(session, admitted, flag)):
            await session.rollback()
            return
        changed = True
        if (stage == "agent" and operation.agent_status != "succeeded"
                and not (operation.agent_status == "failed"
                         and operation.agent_error_code == "evidence_identity_unavailable")):
            operation.agent_status = "queued"
            operation.agent_error_code = f"agent_{code}"
            if malformed:
                operation.agent_cursor = None
                operation.agent_waiting_for_lease = False
            operation.copied_status = "failed"
            operation.copied_error_code = f"agent_{code}"
            operation.status = "failed"
            operation.error_code = f"agent_{code}"
            _hint(hints, operation, "agent-recovery")
        elif stage in {"materialization", "brief"}:
            _fail_copied_stage(operation, stage, f"{stage}_{code}", reset=malformed)
            _hint(hints, operation, f"{stage}-recovery")
        elif (stage == "memory" and operation.memory_status not in {"succeeded"}
                and operation.memory_error_code not in {
                    "legacy_provenance_unresolved", "evidence_identity_unavailable",
                }):
            operation.memory_status = "failed"
            operation.memory_error_code = f"memory_{code}"
            if malformed:
                operation.memory_cursor = None
                operation.memory_unresolved_count = 0
            operation.copied_status = "failed"
            operation.copied_error_code = f"memory_{code}"
            operation.status = "failed"
            operation.error_code = f"memory_{code}"
            _hint(hints, operation, "memory-recovery")
        elif operation.chat_status != "succeeded":
            if malformed:
                operation.copied_cursor = None
            operation.chat_status = "failed"
            operation.chat_error_code = f"chat_{code}"
            operation.copied_status = "failed"
            operation.copied_error_code = f"chat_{code}"
            operation.status = "failed"
            operation.error_code = f"chat_{code}"
            _hint(hints, operation, "chat-recovery")
        elif operation.raw_status not in {"not_present", "retained_shared", "succeeded"}:
            pass  # Only the delivery reschedule below; no stage state to rewrite.
        else:
            changed = False  # Terminal stages do not own a delivery rewrite.
        if not changed or not await _settle(
            session, admitted, "pending", flag, next_attempt_at=datetime.now(UTC) + _RETRY_DELAY,
        ):
            await session.rollback()
            return
        await _commit(session, admitted, flag)
    await _publish_wakeups(factory, flag, hints)


async def process_document_cleanup(ctx: dict[str, object], event_id: str) -> None:
    """Advance raw and copied-evidence cleanup stages after canonical deletion commits.

    Cleanup is intentionally NOT module-gated: a disabled module must still finish deleting data,
    so ``module_is_enabled`` is never consulted here or in the reconcilers.

    Every transaction first admits the claimed event under the receipt's ORIGINAL retained
    authority (never a rebased actor, membership or configuration); missing, legacy or stale
    authority performs no mutation. Raw URI removal commits in its own admission -> privacy ->
    URI -> receipt -> event transaction before copied-owner work. For Agent cleanup, detached
    scope discovery and the exact nonblocking run lease are prepared before the shared privacy and
    receipt locks. Stage cursors commit with owner changes; Source wakeups publish only after
    commit from fresh sessions; Memory cache eviction follows commit under a durable retry marker
    and re-admission. Error recovery compares plain-scalar snapshots to durable state before
    writing; without a snapshot the dispatcher reclaims stale queued work.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    redis = cast(Redis, ctx["redis"])
    flag = _worker_flag(ctx)
    identifier = UUID(event_id)
    attempt = _Attempt()

    try:
        if not await _advance_raw_document_cleanup(factory, settings, flag, identifier):
            return
        if await _advance_copied_cleanup(factory, flag, identifier, attempt):
            await _publish_wakeups(factory, flag, attempt.hints)
            if attempt.evict:
                await _evict_memory_cache_after_commit(factory, redis, flag, attempt)
    except ValueError:
        # A malformed local continuation restarts idempotently from the first exact identity page.
        await _recover_attempt(factory, flag, identifier, attempt, malformed=True)
    except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
        logger.warning("Document copied-evidence cleanup deferred (%s)", type(exc).__name__)
        await _recover_attempt(factory, flag, identifier, attempt, malformed=False)
