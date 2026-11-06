"""Automation run settlement, heartbeat, worker ``process_run`` entry and ``dispatch_runs`` recovery sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.worker_context import worker_session_factory
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope
from modules.automations._execution.actions import (
    _action_step,
)
from modules.automations._execution.runs import (
    _commit,
    _finish,
    _locked_run,
    _skip_pending,
)
from modules.automations._execution.types import (
    DISPATCH_STALE_AFTER,
    MAX_RUN_ATTEMPTS,
    RUNNING_STALE_AFTER,
)
from modules.automations.models import (
    AutomationRun,
    AutomationRunAction,
)
from modules.automations.scope import _actor, _admit
from modules.settings import public as settings_public


async def _settle(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    outcome: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Move the run to the status implied by the last action outcome and persist it."""
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        run = await _locked_run(session, run_id, scope)
        if run is None:
            return "dropped"
        if run.document_evidence_revoked or run.status == "requires_review":
            if run.document_evidence_revoked:
                _finish(run, "requires_review", "document_evidence_revoked")
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            return run.status
        if outcome == "awaiting_approval":
            run.status = "awaiting_approval"
            run.attempts = max(run.attempts - 1, 0)  # waiting for the owner is not a failed pass
        elif outcome == "paused":
            # Module lifecycle pauses work durably without consuming retries or clearing pending actions.
            run.status = "queued"
            run.attempts = max(run.attempts - 1, 0)
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=60)
            run.dispatched_at = None
        elif outcome == "retry":
            run.status = "queued"
            run.dispatched_at = None
            run.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=15 * 2 ** min(run.attempts, 5)
            )
        else:
            status = {
                "succeeded": "succeeded",
                "failed": "failed",
                "dropped": "dropped",
                "requires_review": "requires_review",
            }[outcome]
            _finish(run, status, None if outcome == "succeeded" else outcome)
            if outcome != "succeeded":
                await _skip_pending(session, run_id, "prior_action_" + outcome)
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return run.status


async def _heartbeat(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Refresh ``updated_at`` so a long multi-action run is not mistaken for a dead worker."""
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        run = await _locked_run(session, run_id, scope)
        if run is None:
            raise RuntimeError("invariant violated: run must not be None")
        run.updated_at = datetime.now(UTC)
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def process_run(ctx: dict[str, Any], run_id: str) -> str:
    """Execute (or resume) one queued run: claim it, then run its actions strictly in order.

    Concurrency per rule is 1: a run is claimed only when no sibling run of the same rule is
    ``running``. Each pass counts an attempt (restarts included) so a poisoned run ends as failed
    instead of cycling. The loop stops at the first action that is not ``succeeded``.

    The job carries only the run id: the workspace and owner come from the durable run row, the
    workspace owner must still be that owner (else the row is left untouched, never rebased), and the
    automations module must still be enabled for that workspace.
    """
    factory = worker_session_factory(ctx)
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    rid, now = UUID(run_id), datetime.now(UTC)
    async with factory() as session:
        identity = (
            await session.execute(
                select(AutomationRun.workspace_id, AutomationRun.owner_id).where(
                    AutomationRun.id == rid
                )
            )
        ).first()
        if identity is None:
            return "noop"
        owner = await workspaces.resolve_workspace_owner_context(
            session, identity[0], multi_workspace_enabled=multi_workspace_enabled
        )
        if owner is None or owner.user_id != identity[1]:
            return "noop"
        scope = InternalJobScope(
            workspace_id=identity[0],
            actor_user_id=owner.user_id,
            membership_revision=owner.membership_revision,
        )
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        if not await settings_public.module_is_enabled(
            session,
            "automations",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        ):
            await session.rollback()
            return "paused"
        run = await session.scalar(
            select(AutomationRun)
            .where(
                AutomationRun.workspace_id == scope.workspace_id,
                AutomationRun.owner_id == _actor(scope),
                AutomationRun.id == rid,
            )
            .with_for_update(skip_locked=True)
        )
        if run is None or run.status != "queued" or run.next_attempt_at > now:
            return "noop"
        busy = await session.scalar(
            select(func.count())
            .select_from(AutomationRun)
            .where(
                AutomationRun.workspace_id == scope.workspace_id,
                AutomationRun.owner_id == _actor(scope),
                AutomationRun.automation_id == run.automation_id,
                AutomationRun.id != rid,
                AutomationRun.status == "running",
                AutomationRun.updated_at > now - RUNNING_STALE_AFTER,
            )
        )
        if busy:
            run.next_attempt_at, run.dispatched_at = now + timedelta(seconds=5), None
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            return "busy"
        if run.attempts >= MAX_RUN_ATTEMPTS:
            _finish(run, "failed", "attempts_exhausted")
            await _skip_pending(session, rid, "attempts_exhausted")
            await _commit(
                session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            return "failed"
        run.status, run.attempts = "running", run.attempts + 1
        total = await session.scalar(
            select(func.count())
            .select_from(AutomationRunAction)
            .where(AutomationRunAction.run_id == rid)
        )
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    for ordinal in range(1, (total or 0) + 1):
        await _heartbeat(factory, rid, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        outcome = await _action_step(
            ctx, rid, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if outcome != "succeeded":
            return await _settle(
                factory, rid, outcome, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
    return await _settle(
        factory, rid, "succeeded", scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def dispatch_runs(
    factory: async_sessionmaker[AsyncSession],
    redis: ArqRedis,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Durably (re)enqueue ARQ jobs for runnable runs and expire stale approvals.

    PostgreSQL is the queue authority: a ``queued`` run not dispatched recently, or a ``running``
    run whose worker stopped updating it, is requeued. The ARQ job id carries the dispatch
    generation so a re-dispatch is never swallowed by a finished job of the same run. Returns the
    number of jobs enqueued.
    """
    now = datetime.now(UTC)
    enqueued = 0
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        mine = (
            AutomationRun.workspace_id == scope.workspace_id,
            AutomationRun.owner_id == _actor(scope),
        )
        expired = (
            await session.scalars(
                select(AutomationRunAction)
                .join(
                    AutomationRun,
                    AutomationRun.id == AutomationRunAction.run_id,
                )
                .where(
                    *mine,
                    AutomationRunAction.status == "awaiting_approval",
                    AutomationRunAction.approval_expires_at < now,
                )
                .limit(50)
                .with_for_update(of=AutomationRunAction, skip_locked=True)
            )
        ).all()
        for row in expired:
            row.status, row.error_code = "failed", "approval_expired"
            run = await _locked_run(session, row.run_id, scope)
            if run is None:
                raise RuntimeError("invariant violated: run must not be None")
            _finish(run, "failed", "approval_expired")
            await _skip_pending(session, run.id, "approval_expired")
        runs = (
            await session.scalars(
                select(AutomationRun)
                .where(
                    *mine,
                    (
                        (AutomationRun.status == "queued")
                        & (AutomationRun.next_attempt_at <= now)
                        & (
                            AutomationRun.dispatched_at.is_(None)
                            | (AutomationRun.dispatched_at < now - DISPATCH_STALE_AFTER)
                        )
                    )
                    | (
                        (AutomationRun.status == "running")
                        & (AutomationRun.updated_at < now - RUNNING_STALE_AFTER)
                    ),
                )
                .order_by(AutomationRun.created_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for run in runs:
            run.status = (
                "queued"  # a stale running run resumes; in_flight rows turn review-only there
            )
            run.dispatch_generation += 1
            run.dispatched_at = now
            await redis.enqueue_job(
                "process_automation_run",
                str(run.id),
                _job_id=f"automation-run:{run.id}:{run.dispatch_generation}",
                _defer_until=now,
            )
            enqueued += 1
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return enqueued
