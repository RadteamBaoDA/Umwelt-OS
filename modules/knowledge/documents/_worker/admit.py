"""Cleanup-receipt admission, claim/settle/commit helpers and page-schedule snapshots behind ``modules.knowledge.documents.worker``."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.errors import DomainError
from core.realtime import commit_with_replay
from core.workspaces.public import read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.documents._worker.progress import (
    _attempt_progress_snapshot,
    _source_cleanup_progress_key,
)
from modules.knowledge.documents.models import DocumentCleanupOperation
from modules.knowledge.documents.schemas import DocumentCleanupJobIdentity
from modules.sources import public as sources

# Keep the original logger name so log routing and caplog filters stay unchanged after the split.
logger = logging.getLogger("modules.knowledge.documents.worker")


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


async def _admit_cleanup(
    session: AsyncSession,
    event_id: UUID,
    flag: bool,
    *,
    status: str = "queued",
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
        scope = await ingestion.resolve_ingestion_event_scope(
            session, event_id, multi_workspace_enabled=flag
        )
        if scope is None:
            return None
        event = await ingestion.get_document_cleanup_event(
            session,
            event_id,
            operation_id=operation_id,
            scope=scope,
            multi_workspace_enabled=flag,
        )
        if event is None or event.status != status or event.dispatched_at is None:
            return None
        identity = await documents.read_document_cleanup_job_identity(
            session,
            operation_id,
            scope=scope,
            multi_workspace_enabled=flag,
        )
        if identity is None:
            return None
        original = AccessFence(
            identity.workspace_id,
            identity.actor_user_id,
            identity.membership_revision,
            identity.configuration_revision,
        )
        if await read_access_fence(session, scope=scope, multi_workspace_enabled=flag) != original:
            return None
    except (HTTPException, DomainError):
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
    session: AsyncSession,
    admitted: _Admitted,
    status: Literal["failed", "pending", "delivered"],
    flag: bool,
    *,
    next_attempt_at: datetime | None = None,
    expected_status: Literal["queued", "pending", "delivered", "failed"] = "queued",
) -> bool:
    """CAS the cleanup event on the claim stamp captured at admission; False means the claim moved."""
    return await ingestion.settle_document_cleanup_event_in_uow(
        session,
        admitted.event_id,
        status,
        operation_id=admitted.identity.operation_id,
        dispatched_at=admitted.dispatched_at,
        next_attempt_at=next_attempt_at,
        expected_status=expected_status,
        scope=admitted.scope,
        multi_workspace_enabled=flag,
    )


async def _commit(session: AsyncSession, admitted: _Admitted, flag: bool) -> None:
    """Commit the cleanup transaction through the replay path under the ORIGINAL admitted access fence."""
    await commit_with_replay(
        session,
        (),
        scope=admitted.scope,
        multi_workspace_enabled=flag,
        access_fence=admitted.original,
    )


async def _lock_claim(session: AsyncSession, admitted: _Admitted, flag: bool) -> bool:
    """Lock the single cleanup outbox row and require the original queued claim."""
    event = await ingestion.lock_document_cleanup_event_in_uow(
        session,
        admitted.event_id,
        operation_id=admitted.identity.operation_id,
        scope=admitted.scope,
        multi_workspace_enabled=flag,
    )
    return (
        event is not None
        and event.status == "queued"
        and event.dispatched_at == admitted.dispatched_at
    )


def _hint(
    hints: list[documents.SourceCleanupWakeup], operation: DocumentCleanupOperation, stage: str
) -> None:
    """Snapshot a content-free Source wakeup hint while the receipt is held; publish only after commit."""
    hints.append(
        documents.source_cleanup_wakeup_hint(
            operation,
            progress_key=_source_cleanup_progress_key(operation, stage),
        )
    )


async def _publish_wakeups(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    hints: list[documents.SourceCleanupWakeup],
) -> None:
    """Wake each open Source-purge observer from a fresh lock-free session; failures only log.

    The durable fallback is the Sources coverage reconciler, so nothing here may fail a stage.
    """
    for hint in hints:
        try:
            async with factory() as session:
                observers = list(
                    dict.fromkeys(
                        ([hint.linked_operation_id] if hint.linked_operation_id else [])
                        + list(
                            await sources.discover_source_purge_observer_ids(
                                session,
                                workspace_id=hint.workspace_id,
                                source_id=hint.source_id,
                                limit=100,
                            )
                        ),
                    )
                )
                await session.rollback()
        except Exception as exc:  # noqa: BLE001  # boundary: durable reconciler is the fallback
            logger.warning("Source cleanup wakeup discovery deferred (%s)", type(exc).__name__)
            continue
        for observer in observers:
            try:
                async with factory() as session:
                    await documents.publish_source_cleanup_wakeup(
                        session,
                        hint,
                        observer,
                        multi_workspace_enabled=flag,
                    )
            except Exception as exc:  # noqa: BLE001  # boundary: durable reconciler is the fallback
                logger.warning("Source cleanup wakeup deferred (%s)", type(exc).__name__)


async def _requeue(
    factory: async_sessionmaker[AsyncSession],
    flag: bool,
    admitted: _Admitted,
    delay: timedelta,
) -> None:
    """Reschedule the exact original claim from a fresh admission after dropping all locks."""
    async with factory() as session:
        if await _admit_cleanup(session, admitted.event_id, flag) != admitted:
            await session.rollback()
            return
        if await _settle(
            session, admitted, "pending", flag, next_attempt_at=datetime.now(UTC) + delay
        ):
            await _commit(session, admitted, flag)
        else:
            await session.rollback()


def _cursor_state_agent(cursor: object) -> dict[str, object]:
    """Validate a stored Agent cleanup cursor (or return the initial one); ``ValueError`` if malformed."""
    state = cursor or {
        "v": 1,
        "reference_after": None,
        "candidate_after": None,
        "phase": "discover",
    }
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "v",
            "reference_after",
            "candidate_after",
            "phase",
        }
        or state.get("v") != 1
        or state.get("phase") not in {"discover", "finalize"}
    ):
        raise ValueError("Stored Agent cleanup cursor is malformed")
    return state


@dataclass
class _PageSchedule:
    """Retry schedule shared by the stage helpers of one locked copied-cleanup page.

    ``next_attempt_at`` is the earliest deferral any stage asked for (``None`` means the event is
    complete for Documents and is settled ``delivered``). ``terminal_scope_failure`` marks the
    truthful, unretryable "evidence identities unavailable" outcome.
    """

    next_attempt_at: datetime | None = None
    terminal_scope_failure: bool = False

    def defer(self, delay: timedelta) -> None:
        """Keep the first requested deferral; later stages only fill a missing one."""
        self.next_attempt_at = self.next_attempt_at or datetime.now(UTC) + delay


@dataclass(frozen=True)
class _ReceiptSnapshot:
    """Scalar fields of the unlocked receipt read, compared again under the receipt lock."""

    operation_id: UUID
    source_id: UUID
    document_id: UUID
    evidence_scope_status: str
    agent_status: str
    agent_cursor: object

    @classmethod
    def of(cls, operation: DocumentCleanupOperation) -> "_ReceiptSnapshot":
        """Capture plain scalars (and a copy of the cursor) so later attribute reads are safe."""
        return cls(
            operation_id=operation.id,
            source_id=operation.source_id,
            document_id=operation.document_id,
            evidence_scope_status=operation.evidence_scope_status,
            agent_status=operation.agent_status,
            agent_cursor=(
                dict(operation.agent_cursor)
                if isinstance(operation.agent_cursor, dict)
                else operation.agent_cursor
            ),
        )

    def differs_from(self, operation: DocumentCleanupOperation) -> bool:
        """Return whether the locked receipt moved since the unlocked preflight read."""
        return (
            operation.id != self.operation_id
            or operation.source_id != self.source_id
            or operation.document_id != self.document_id
            or operation.evidence_scope_status != self.evidence_scope_status
            or operation.agent_status != self.agent_status
            or operation.agent_cursor != self.agent_cursor
        )


@dataclass(frozen=True)
class _AgentPreflight:
    """Detached Agent evidence page and lease preflight taken before any privacy/receipt lock."""

    evidence: documents.DocumentCleanupEvidenceScope
    result: Any
    reference_after: UUID | None


async def _agent_preflight(
    session: AsyncSession,
    operation_hint: DocumentCleanupOperation,
    attempt: _Attempt,
    *,
    operation_id: UUID,
    job_scope: InternalJobScope,
    flag: bool,
) -> _AgentPreflight | None:
    """Prepare the Agent lease preflight for a queued/running Agent stage; ``None`` if not due.

    Runs before the shared privacy or receipt lock (a successful transaction advisory lease is
    retained by the same session through commit). Preflight failures (bad cursor, lease DB errors)
    need Agent-stage recovery, so ``attempt.stage``/``attempt.progress`` are set first; the unlocked
    snapshot lets recovery compare under the receipt lock before writing. Raises ``ValueError`` for
    a malformed stored cursor or unavailable evidence scope.
    """
    if not (
        operation_hint.agent_status in {"queued", "running"}
        and operation_hint.evidence_scope_status == "captured"
    ):
        return None
    attempt.stage = "agent"
    attempt.progress = _attempt_progress_snapshot(operation_hint)
    cursor_state = _cursor_state_agent(operation_hint.agent_cursor)
    reference_value = cursor_state.get("reference_after")
    candidate_cursor = cursor_state.get("candidate_after")
    if reference_value is not None and not isinstance(reference_value, str):
        raise ValueError("Stored Agent reference cursor is malformed")
    if candidate_cursor is not None and not isinstance(candidate_cursor, str):
        raise ValueError("Stored Agent candidate cursor is malformed")
    reference_after = UUID(reference_value) if reference_value else None
    evidence = await documents.list_document_cleanup_evidence_scope(
        session,
        operation_id,
        after=reference_after,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    if evidence is None:
        raise ValueError("Document cleanup evidence scope is unavailable")
    from modules.agents import public as agents

    result = await agents.preflight_document_copied_evidence_lease(
        session,
        evidence,
        cursor=candidate_cursor,
        limit=100,
        scope=job_scope,
        multi_workspace_enabled=flag,
    )
    return _AgentPreflight(evidence=evidence, result=result, reference_after=reference_after)
