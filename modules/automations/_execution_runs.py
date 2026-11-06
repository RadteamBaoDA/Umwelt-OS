"""Automation trigger fan-out, manual starts, run listing and owner approval decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.realtime import commit_with_replay
from core.workspaces.schemas import AccessFence, Scope
from modules.automations._execution_core import (
    dependencies_missing,
    live_rules,
    plan_run,
)
from modules.automations._execution_evidence import (
    _document_trigger_sources,
    _run_evidence_current,
    _scrub_document_payload,
    _trigger_evidence_current,
)
from modules.automations._execution_types import (
    RunConflict,
    RunMissing,
)
from modules.automations.models import (
    Automation,
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
    AutomationTrigger,
)
from modules.automations.schemas import RunActionRead, RunPage, RunRead
from modules.automations.scope import _actor, _admit
from modules.tools.public import webhook_profile_revision


async def fan_out_triggers(
    factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Turn pending inbox events into runs, one event at a time under row locks.

    The inbox row is marked processed in the same transaction as the runs it produced, so a crash
    re-reads the event and the run identity index absorbs the repeat. Exact Document provenance is
    revalidated before planning; unavailable events are scrubbed and made ineligible. A detached
    bounded page discovers and prelocks proved Source IDs in UUID order before inbox row locks; if a
    new page member requires an unheld Source, the batch is rolled back and retried later.
    """
    created = 0
    async with factory() as session:
        # The access fence is the first statement: Source locks below then use it as their expected fence.
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        pending = (
            AutomationTrigger.workspace_id == scope.workspace_id,
            AutomationTrigger.owner_id == _actor(scope),
            AutomationTrigger.status == "pending",
        )
        # Discover the bounded page without owner locks, then acquire its Source fences in UUID order.
        detached = (
            await session.scalars(
                select(AutomationTrigger)
                .where(*pending)
                .order_by(AutomationTrigger.created_at)
                .limit(100)
            )
        ).all()
        source_ids = (
            await _document_trigger_sources(
                session,
                detached,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            or set()
        )
        from modules.sources import public as sources

        for source_id in sorted(source_ids, key=str):
            await sources.lock_retained_evidence_source(
                session,
                source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=fence,
            )
        events = (
            await session.scalars(
                select(AutomationTrigger)
                .where(*pending)
                .order_by(AutomationTrigger.created_at)
                .limit(100)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).all()
        # A producer may have inserted a new page member between discovery and row locking. Do not
        # discover and acquire its Source fence while holding inbox locks; defer the whole batch.
        if (
            await _document_trigger_sources(
                session,
                events,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                held=source_ids,
            )
            is None
        ):
            await session.rollback()
            return 0
        for event in events:
            if event.trigger_type == "new_document" and not await _trigger_evidence_current(
                session,
                event,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            ):
                event.status = "processed"
                event.document_evidence_revoked = True
                event.payload = _scrub_document_payload(event.payload)
                continue
            for rev in await live_rules(session, event.trigger_type, scope=scope):
                if event.trigger_type == "webhook" and rev.trigger.get("hook") != event.payload.get(
                    "hook"
                ):
                    continue
                run_id = await plan_run(
                    session,
                    rev=rev,
                    trigger_type=event.trigger_type,
                    trigger_key=f"event:{event.event_key}",
                    trigger_event_id=event.event_key,
                    slot=None,
                    payload=event.payload,
                    depth=event.depth + 1,
                    document_id=event.document_id,
                    document_version_id=event.document_version_id,
                    origin_automation_id=event.origin_automation_id,
                    origin_run_id=event.origin_run_id,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
                created += run_id is not None
            event.status = "processed"
        await commit_with_replay(
            session,
            [],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
    return created


async def start_manual(
    session: AsyncSession,
    automation_id: UUID,
    expected_revision: int,
    client_request_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RunRead:
    """Queue one run of a stored enabled rule at the expected revision (idempotent on the client id).

    Conditions are not applied (there is no event); admission limits and approvals still are.

    Raises:
        RunMissing: Rule absent, deleted or foreign.
        RunConflict: Stale revision or the rule is paused.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    head = await session.scalar(
        select(Automation)
        .where(
            Automation.workspace_id == scope.workspace_id,
            Automation.owner_id == _actor(scope),
            Automation.id == automation_id,
            Automation.deleted_at.is_(None),
        )
        .with_for_update(read=True)
    )
    if head is None:
        raise RunMissing
    if head.revision != expected_revision:
        raise RunConflict("stale_revision", f"Rule is at revision {head.revision}", head.revision)
    if not head.enabled:
        raise RunConflict("paused", "Rule is paused")
    rev = await session.scalar(
        select(AutomationRevision).where(
            AutomationRevision.automation_id == head.id,
            AutomationRevision.revision == head.revision,
        )
    )
    key = f"manual:{client_request_id}"
    # the head's current revision snapshot always exists
    if rev is None:
        raise RuntimeError("invariant violated: rev must not be None")
    await plan_run(
        session,
        rev=rev,
        trigger_type="manual",
        trigger_key=key,
        trigger_event_id=None,
        slot=None,
        payload={},
        depth=1,
        origin_automation_id=None,
        origin_run_id=None,
        apply_conditions=False,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session,
        [],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    run = await session.scalar(
        select(AutomationRun).where(
            AutomationRun.workspace_id == scope.workspace_id,
            AutomationRun.owner_id == _actor(scope),
            AutomationRun.automation_id == head.id,
            AutomationRun.revision == head.revision,
            AutomationRun.trigger_key == key,
        )
    )
    # plan_run committed the manual run above
    if run is None:
        raise RuntimeError("invariant violated: run must not be None")
    return (await _reads(session, [run]))[0]


async def _reads(session: AsyncSession, runs: list[AutomationRun]) -> list[RunRead]:
    """Project runs with their ordered action outcomes (codes and references only)."""
    if not runs:
        return []
    actions = (
        await session.scalars(
            select(AutomationRunAction)
            .where(AutomationRunAction.run_id.in_([r.id for r in runs]))
            .order_by(AutomationRunAction.run_id, AutomationRunAction.ordinal)
        )
    ).all()
    by_run: dict[UUID, list[RunActionRead]] = {}
    for a in actions:
        by_run.setdefault(a.run_id, []).append(
            RunActionRead(
                ordinal=a.ordinal,
                type=a.action_type,
                status=a.status,
                attempts=a.attempts,
                error_code=a.error_code,
                result_reference=a.result_reference,
                approval_expires_at=a.approval_expires_at,
            )
        )
    return [
        RunRead(
            id=r.id,
            automation_id=r.automation_id,
            revision=r.revision,
            trigger_type=r.trigger_type,
            trigger_event_id=r.trigger_event_id,
            scheduled_slot=r.scheduled_slot,
            depth=r.depth,
            origin_automation_id=r.origin_automation_id,
            origin_run_id=r.origin_run_id,
            status=r.status,
            reason=r.reason,
            attempts=r.attempts,
            created_at=r.created_at,
            finished_at=r.finished_at,
            actions=by_run.get(r.id, []),
        )
        for r in runs
    ]


async def list_runs(
    session: AsyncSession,
    automation_id: UUID,
    limit: int = 50,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RunPage:
    """Newest-first run history for a rule, retained after pause or delete (bounded to 100)."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    exists = await session.scalar(
        select(Automation.id).where(
            Automation.workspace_id == scope.workspace_id,
            Automation.owner_id == _actor(scope),
            Automation.id == automation_id,
        )
    )
    if exists is None:
        raise RunMissing
    runs = (
        await session.scalars(
            select(AutomationRun)
            .where(
                AutomationRun.workspace_id == scope.workspace_id,
                AutomationRun.owner_id == _actor(scope),
                AutomationRun.automation_id == automation_id,
            )
            .order_by(AutomationRun.created_at.desc())
            .limit(min(max(limit, 1), 100))
        )
    ).all()
    return RunPage(items=await _reads(session, list(runs)))


def _approval_hash(
    run: AutomationRun, ordinal: int, spec: Mapping[str, Any], destination: str | None
) -> str:
    """Bind an approval to the run, slot, immutable revision, exact action and webhook destination digest."""
    blob = json.dumps(
        [str(run.id), ordinal, run.revision, dict(spec), destination],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


async def _fence_current(
    session: AsyncSession, run: AutomationRun, *, scope: Scope, share: bool = False
) -> bool:
    """True while the rule is live, enabled and still at the run's revision.

    ``share=True`` takes ``FOR SHARE`` on the head so an edit (which locks ``FOR UPDATE``) waits for
    the current action transaction; in-database effects therefore commit before any newer revision.
    """
    stmt = select(Automation).where(
        Automation.workspace_id == scope.workspace_id,
        Automation.owner_id == _actor(scope),
        Automation.id == run.automation_id,
    )
    head = await session.scalar(stmt.with_for_update(read=True) if share else stmt)
    return (
        head is not None
        and head.deleted_at is None
        and head.enabled
        and head.revision == run.revision
        and head.owner_id == run.owner_id
    )


def _finish(run: AutomationRun, status: str, reason: str | None) -> None:
    """Set a terminal run status; the caller also skips pending actions via ``_skip_pending``."""
    run.status, run.reason, run.finished_at = status, reason, datetime.now(UTC)


async def _skip_pending(session: AsyncSession, run_id: UUID, code: str) -> None:
    """Mark every not-yet-started action of a finished run skipped so history is explicit."""
    rows = await session.scalars(
        select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id,
            AutomationRunAction.status.in_(("pending", "approved", "awaiting_approval")),
        )
    )
    for row in rows:
        row.status, row.error_code, row.approved_session_hash = "skipped", code, None


def _destination_stale(settings: Settings, spec: Mapping[str, Any], bound: str | None) -> bool:
    """True when a webhook action's alias no longer resolves to the destination digest that was approved."""
    if spec["type"] != "call_webhook":
        return False
    try:
        return webhook_profile_revision(settings, spec["alias"]) != bound
    except ValueError:
        return True


async def decide_action(
    session: AsyncSession,
    session_hash: str,
    run_id: UUID,
    ordinal: int,
    approve: bool,
    settings: Settings,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> RunRead:
    """Record the owner's decision on one action waiting for approval.

    Approval is bound to the action definition of the cited revision (``approval_hash``), expires
    after 24 hours and is void if the rule changed, was paused or deleted (revision fence). Document
    trigger evidence is revalidated before either decision. On approve the owner session digest is
    kept only until the worker performs the action. Nothing is executed here; the worker acts after commit.

    Raises:
        RunMissing: Unknown run or action.
        RunConflict: Not waiting, expired or fenced out.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    run = await session.scalar(
        select(AutomationRun)
        .where(
            AutomationRun.workspace_id == scope.workspace_id,
            AutomationRun.owner_id == _actor(scope),
            AutomationRun.id == run_id,
        )
        .with_for_update()
    )
    row = (
        None
        if run is None
        else await session.scalar(
            select(AutomationRunAction)
            .where(AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal)
            .with_for_update()
        )
    )
    if run is None or row is None:
        raise RunMissing
    if run.status != "awaiting_approval" or row.status != "awaiting_approval":
        raise RunConflict("not_pending", "Action is not waiting for approval")
    now = datetime.now(UTC)
    rev = await session.scalar(
        select(AutomationRevision).where(
            AutomationRevision.automation_id == run.automation_id,
            AutomationRevision.revision == run.revision,
        )
    )
    spec = rev.actions[ordinal - 1] if rev is not None and ordinal <= len(rev.actions) else None
    problem = None
    if row.approval_expires_at is None or row.approval_expires_at <= now:
        problem = ("approval_expired", "Approval expired")
    elif spec is None or row.approval_hash != _approval_hash(
        run, ordinal, spec, row.destination_revision
    ):
        problem = ("approval_mismatch", "Approval no longer matches the action")
    elif not await _run_evidence_current(
        session,
        run,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    ):
        problem = (
            "document_evidence_unavailable",
            "Document trigger evidence is no longer available",
        )
    elif not await _fence_current(session, run, scope=scope):
        problem = ("stale_revision", "Rule changed after this run was queued")
    elif _destination_stale(settings, spec, row.destination_revision):
        problem = (
            "stale_destination",
            "Webhook destination changed after this approval was requested",
        )
    elif rev is not None and dependencies_missing(rev):
        problem = ("dependency_unavailable", "A required module is unavailable")
    if problem is not None:
        dropped = problem[0] in (
            "stale_revision",
            "stale_destination",
            "dependency_unavailable",
            "document_evidence_unavailable",
        )
        row.status, row.error_code = ("skipped" if dropped else "failed"), problem[0]
        _finish(run, "dropped" if dropped else "failed", problem[0])
        await _skip_pending(session, run.id, problem[0])
        await commit_with_replay(
            session,
            [],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        )
        raise RunConflict(problem[0], problem[1])
    if approve:
        row.status, row.approved_session_hash = "approved", session_hash
        run.status, run.next_attempt_at, run.dispatched_at = "queued", now, None
    else:
        row.status, row.error_code = "denied", "denied_by_owner"
        _finish(run, "failed", "denied")
        await _skip_pending(session, run.id, "denied")
    await commit_with_replay(
        session,
        [],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return (await _reads(session, [run]))[0]


async def _commit(
    session: AsyncSession, fence: AccessFence, *, scope: Scope, multi_workspace_enabled: bool
) -> None:
    """Fenced commit for a transaction that took the access fence first."""
    await commit_with_replay(
        session,
        [],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )


async def _locked_run(session: AsyncSession, run_id: UUID, scope: Scope) -> AutomationRun | None:
    """FOR UPDATE select of one run inside the scope's workspace and owner."""
    return await session.scalar(
        select(AutomationRun)
        .where(
            AutomationRun.workspace_id == scope.workspace_id,
            AutomationRun.owner_id == _actor(scope),
            AutomationRun.id == run_id,
        )
        .with_for_update()
    )


async def _mark(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    ordinal: int,
    status: str,
    code: str | None = None,
    reference: str | None = None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Persist an action outcome after locking its run then action, preserving revocation review state."""
    async with factory() as session:
        fence = await _admit(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
        )
        run = await _locked_run(session, run_id, scope)
        row = await session.scalar(
            select(AutomationRunAction)
            .where(AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal)
            .with_for_update()
        )
        if row is not None:
            if run is not None and (
                run.document_evidence_revoked or run.status == "requires_review"
            ):
                if row.status != "succeeded":
                    row.status = "requires_review"
                    row.error_code = (
                        "document_evidence_revoked"
                        if run.document_evidence_revoked
                        else (row.error_code or "run_requires_review")
                    )
                if reference is not None:
                    row.result_reference = reference
                row.approved_session_hash = None
            else:
                row.status, row.error_code, row.result_reference = status, code, reference
                row.approved_session_hash = None
            await commit_with_replay(
                session,
                [],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=fence,
            )
