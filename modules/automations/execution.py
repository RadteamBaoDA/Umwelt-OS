"""Durable automation dispatch: trigger inbox, run planning, ordered action execution and recovery.

Invariants (read before changing anything here):

* Identity and dedupe. A run is identified by ``(automation_id, revision, trigger_key)`` where the
  key is the trigger event id, the scheduled slot or a manual client id. The unique index is the
  only dedupe fence; every producer path inserts with ON CONFLICT DO NOTHING.
* Revision fence. A run cites an immutable revision. Before each action, and again immediately
  before an external write, the rule head must still be live, enabled and at that revision. Edits,
  pauses and deletes all bump the revision, so "revision differs" is the single drop condition.
* Loop bounds. Runs carry ``depth`` (root = 1) and origin ids; depth above ``MAX_DEPTH``, a rule
  reacting to its own output, known loop pairs, the cooldown and an hourly rate cap all end in a
  recorded ``skipped`` run instead of work.
* No replay of ambiguous effects. External and non-atomic actions are committed ``in_flight`` before
  the call. A row found ``in_flight`` after a restart becomes ``requires_review`` and is never
  re-sent. In-database actions (task, notification) commit together with their outcome row, so they
  are exactly-once.
* Approvals. ``run_agent`` and ``call_webhook`` always stop at ``awaiting_approval``; only an
  owner decision (owner session, CSRF) lets the worker perform them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from arq.connections import ArqRedis
from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from modules.automations.conditions import TRIGGER_FIELDS, evaluate, validate_sample
from modules.automations.models import (
    Automation,
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
    AutomationTrigger,
)
from modules.automations.schemas import RunActionRead, RunPage, RunRead
from modules.agents import public as agents
from modules.agents.public import ProfileRunStart
from modules.chat.public import Conversation
from modules.dashboard import public as dashboard
from modules.notifications.public import NotificationEmit, emit
from modules.settings import public as settings_public
from modules.tasks.public import TaskConflict, TaskCreate, create_task_in_uow
from modules.tools.public import send_webhook_once, webhook_profile_revision

MAX_DEPTH = 5  # chain depth of automation-caused events
MAX_ACTIONS = 10
COOLDOWN_SECONDS = 60  # per-rule default between non-scheduled runs
MAX_RUNS_PER_HOUR = 30  # per-rule rate cap (scheduled runs included)
MAX_RUN_ATTEMPTS = 4  # worker passes over one run, including restarts
MAX_ACTION_ATTEMPTS = 3  # transient retries of one retry-safe action
DISPATCH_STALE_AFTER = timedelta(seconds=30)
RUNNING_STALE_AFTER = timedelta(seconds=200)  # longer than the 180 s ARQ job timeout
OWNER_ID = 1
APPROVAL_ACTIONS = frozenset({"run_agent", "call_webhook"})
# Module that must be registered and enabled for each trigger / action type.
TRIGGER_MODULE = {
    "schedule": None, "new_event": "knowledge.timeline", "new_document": "knowledge.documents",
    "entity_changed": "knowledge.entities", "task_due": "tasks", "goal_deadline": "goals",
    "webhook": "tools", "connector_sync_result": "sources",
}
ACTION_MODULE = {
    "run_agent": "agents", "create_task": "tasks", "create_notification": "notifications",
    "generate_brief": "dashboard", "call_webhook": "tools",
}
_modules_cache: dict[str, Any] = {}
_TERMINAL_RUN = ("succeeded", "failed", "skipped", "dropped", "requires_review")


class RunMissing(Exception):
    """Raised for an absent or foreign-owned run or rule."""


class RunConflict(Exception):
    """Raised when a request no longer matches the run, action or rule state."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep a stable machine code and optional current revision for HTTP 409."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


def loop_guard(trigger: Mapping[str, Any], actions: list[Mapping[str, Any]]) -> str | None:
    """Name a known self-feeding trigger/action pair, or None.

    Pairs: ``task_due`` + ``create_task`` whose new task is already inside the lead window
    (re-fires every cycle); ``entity_changed`` + ``run_agent`` (the agent can write entities);
    ``webhook`` + ``call_webhook`` (the outbound endpoint can post back to the inbound hook).
    """
    kinds = {a["type"] for a in actions}
    kind = trigger["type"]
    if kind == "task_due" and any(
        a["type"] == "create_task" and a.get("due_in_days") is not None
        and a["due_in_days"] * 1440 <= trigger.get("lead_minutes", 0) for a in actions
    ):
        return "loop_task_due_create_task"
    if kind == "entity_changed" and "run_agent" in kinds:
        return "loop_entity_changed_run_agent"
    if kind == "webhook" and "call_webhook" in kinds:
        return "loop_webhook_call_webhook"
    return None


async def enqueue_trigger(
    session: AsyncSession, owner_id: int, trigger_type: str, event_key: str, payload: Mapping[str, Any],
    *, hook: str | None = None, origin_automation_id: UUID | None = None, origin_run_id: UUID | None = None,
    depth: int = 0,
) -> bool:
    """Offer one trigger event to the automation inbox inside the producer's transaction.

    ``event_key`` must be the producer's stable event id; re-offering it is a no-op (returns False),
    which is the first dedupe layer. ``payload`` carries only metadata fields declared for the
    trigger (see ``TRIGGER_FIELDS``) and never content. Events caused by an automation must pass
    their ``origin_*`` ids and the causing run's ``depth``. Nothing is committed here.

    Raises:
        ValueError: Unknown trigger, bad key, undeclared payload field or missing webhook hook.
    """
    if trigger_type not in TRIGGER_FIELDS or trigger_type == "schedule":
        raise ValueError("trigger type cannot be offered by a producer")
    if not 1 <= len(event_key) <= 200 or not 0 <= depth <= 50:
        raise ValueError("invalid event key or depth")
    validate_sample(trigger_type, dict(payload))
    stored = dict(payload)
    if trigger_type == "webhook":
        if not hook:
            raise ValueError("webhook triggers need a hook name")
        stored["hook"] = hook
    result = await session.execute(
        insert(AutomationTrigger).values(
            id=uuid4(), owner_id=owner_id, trigger_type=trigger_type, event_key=event_key, payload=stored,
            depth=depth, origin_automation_id=origin_automation_id, origin_run_id=origin_run_id, status="pending")
        .on_conflict_do_nothing(constraint="uq_automation_triggers_event").returning(AutomationTrigger.id))
    return result.scalar_one_or_none() is not None


def dependencies_missing(rev: AutomationRevision) -> list[str]:
    """Module ids this revision needs that are not registered and enabled right now.

    Descriptors are static, so this is cheap; it is checked at plan time and again before every
    action so a module disabled after save stops queued work (grant revalidation).
    """
    if "registry" not in _modules_cache:
        from core.modules import register_modules  # lazy: core.modules imports module descriptors

        _modules_cache["registry"] = register_modules()
    registry = _modules_cache["registry"]
    needed = {TRIGGER_MODULE[rev.trigger["type"]]} | {ACTION_MODULE[a["type"]] for a in rev.actions}
    return sorted(m for m in needed if m is not None and (m not in registry or not registry[m].enabled))


async def origin_for_reference(session: AsyncSession, reference: str) -> tuple[UUID, UUID, int] | None:
    """Map an effect reference such as ``task:<id>`` to ``(automation_id, run_id, depth)`` of its creator.

    Producers call this before offering a trigger so an event caused by an automation continues
    that run's causal chain (depth + 1, origin ids) instead of looking like a root event.
    """
    row = (await session.execute(
        select(AutomationRun.automation_id, AutomationRun.id, AutomationRun.depth)
        .join(AutomationRunAction, AutomationRunAction.run_id == AutomationRun.id)
        .where(AutomationRunAction.result_reference == reference, AutomationRunAction.status == "succeeded")
        .limit(1))).first()
    return None if row is None else (row[0], row[1], row[2])


async def _admission(
    session: AsyncSession, rev: AutomationRevision, trigger_type: str, depth: int,
    origin_automation_id: UUID | None, now: datetime,
) -> tuple[str | None, datetime]:
    """Return ``(skip_reason, run_at)``: a reason records a skipped run; otherwise ``run_at`` is when to run.

    Static loop pairs, missing modules, depth, self-origin and the hourly cap skip. The cooldown
    does not drop work: non-scheduled runs are queued behind the rule's latest planned run
    (``+ COOLDOWN_SECONDS``), so a burst is spaced out instead of lost. Scheduled slots are
    exempt; their floor is the cron interval.
    """
    if loop_guard(rev.trigger, rev.actions):
        return "loop_pair", now
    if dependencies_missing(rev):
        return "dependency_unavailable", now
    if depth > MAX_DEPTH:
        return "depth_exceeded", now
    if origin_automation_id == rev.automation_id:
        return "self_origin", now
    base = (AutomationRun.automation_id == rev.automation_id, AutomationRun.status != "skipped")
    hourly = await session.scalar(select(func.count()).select_from(AutomationRun).where(
        *base, AutomationRun.created_at > now - timedelta(hours=1)))
    if (hourly or 0) >= MAX_RUNS_PER_HOUR:
        return "rate_limited", now
    if trigger_type == "schedule":
        return None, now
    last = await session.scalar(select(func.max(AutomationRun.next_attempt_at)).where(*base))
    run_at = last + timedelta(seconds=COOLDOWN_SECONDS) if last is not None else now
    return None, max(run_at, now)


async def plan_run(
    session: AsyncSession, *, owner_id: int, rev: AutomationRevision, trigger_type: str, trigger_key: str,
    trigger_event_id: str | None, slot: datetime | None, payload: Mapping[str, Any], depth: int,
    origin_automation_id: UUID | None, origin_run_id: UUID | None, apply_conditions: bool = True,
) -> UUID | None:
    """Create the queued (or recorded-skipped) run for one trigger, or None when nothing is created.

    None means conditions did not match or this identity already exists (duplicate event, retry or
    double slot). The insert is ON CONFLICT DO NOTHING on ``uq_automation_runs_identity`` so two
    concurrent planners cannot both succeed. Action rows are created up front (<= 10) so every
    outcome has a durable slot. The caller commits.
    """
    if apply_conditions and rev.conditions and not evaluate(rev.conditions, dict(payload))[0]:
        return None
    now = datetime.now(UTC)
    reason, run_at = await _admission(session, rev, trigger_type, depth, origin_automation_id, now)
    run_id = uuid4()
    result = await session.execute(
        insert(AutomationRun).values(
            id=run_id, owner_id=owner_id, automation_id=rev.automation_id, revision=rev.revision,
            trigger_type=trigger_type, trigger_key=trigger_key, trigger_event_id=trigger_event_id,
            scheduled_slot=slot, depth=min(depth, 50), origin_automation_id=origin_automation_id,
            origin_run_id=origin_run_id, status="skipped" if reason else "queued", reason=reason,
            payload=dict(payload), attempts=0, dispatch_generation=0, next_attempt_at=run_at,
            finished_at=now if reason else None)
        .on_conflict_do_nothing(constraint="uq_automation_runs_identity").returning(AutomationRun.id))
    if result.scalar_one_or_none() is None:
        return None
    if not reason:
        session.add_all(
            AutomationRunAction(id=uuid4(), run_id=run_id, ordinal=i, action_type=a["type"], status="pending", attempts=0)
            for i, a in enumerate(rev.actions[:MAX_ACTIONS], start=1))
    return run_id


async def live_rules(session: AsyncSession, trigger_type: str) -> list[AutomationRevision]:
    """Current snapshots of live, enabled rules for one trigger type (bounded to 100)."""
    rows = await session.execute(
        select(AutomationRevision).join(
            Automation,
            (AutomationRevision.automation_id == Automation.id) & (AutomationRevision.revision == Automation.revision),
        ).where(
            Automation.owner_id == OWNER_ID, Automation.deleted_at.is_(None), Automation.enabled.is_(True),
            AutomationRevision.trigger["type"].astext == trigger_type).limit(100))
    return list(rows.scalars().all())


async def run_exists(session: AsyncSession, rev: AutomationRevision, trigger_key: str) -> bool:
    """Cheap pre-check so sweeps do not re-plan identities that already have a run (dedupe stays the index)."""
    return await session.scalar(select(AutomationRun.id).where(
        AutomationRun.automation_id == rev.automation_id, AutomationRun.revision == rev.revision,
        AutomationRun.trigger_key == trigger_key).limit(1)) is not None


async def fan_out_triggers(factory: async_sessionmaker[AsyncSession]) -> int:
    """Turn pending inbox events into runs, one event at a time under row locks.

    The inbox row is marked processed in the same transaction as the runs it produced, so a crash
    re-reads the event and the run identity index absorbs the repeat. Returns runs created.
    """
    created = 0
    async with factory() as session:
        events = (await session.scalars(
            select(AutomationTrigger).where(AutomationTrigger.status == "pending")
            .order_by(AutomationTrigger.created_at).limit(100).with_for_update(skip_locked=True))).all()
        for event in events:
            for rev in await live_rules(session, event.trigger_type):
                if event.trigger_type == "webhook" and rev.trigger.get("hook") != event.payload.get("hook"):
                    continue
                run_id = await plan_run(
                    session, owner_id=event.owner_id, rev=rev, trigger_type=event.trigger_type,
                    trigger_key=f"event:{event.event_key}", trigger_event_id=event.event_key, slot=None,
                    payload=event.payload, depth=event.depth + 1,
                    origin_automation_id=event.origin_automation_id, origin_run_id=event.origin_run_id)
                created += run_id is not None
            event.status = "processed"
        await session.commit()
    return created


async def start_manual(
    session: AsyncSession, owner_id: int, automation_id: UUID, expected_revision: int, client_request_id: UUID,
) -> RunRead:
    """Queue one run of a stored enabled rule at the expected revision (idempotent on the client id).

    Conditions are not applied (there is no event); admission limits and approvals still are.

    Raises:
        RunMissing: Rule absent, deleted or foreign.
        RunConflict: Stale revision or the rule is paused.
    """
    head = await session.scalar(select(Automation).where(
        Automation.id == automation_id, Automation.owner_id == owner_id, Automation.deleted_at.is_(None)
    ).with_for_update(read=True))
    if head is None:
        raise RunMissing
    if head.revision != expected_revision:
        raise RunConflict("stale_revision", f"Rule is at revision {head.revision}", head.revision)
    if not head.enabled:
        raise RunConflict("paused", "Rule is paused")
    rev = await session.scalar(select(AutomationRevision).where(
        AutomationRevision.automation_id == head.id, AutomationRevision.revision == head.revision))
    key = f"manual:{client_request_id}"
    await plan_run(
        session, owner_id=owner_id, rev=rev, trigger_type="manual", trigger_key=key, trigger_event_id=None,
        slot=None, payload={}, depth=1, origin_automation_id=None, origin_run_id=None, apply_conditions=False)
    await session.commit()
    run = await session.scalar(select(AutomationRun).where(
        AutomationRun.automation_id == head.id, AutomationRun.revision == head.revision,
        AutomationRun.trigger_key == key))
    return (await _reads(session, [run]))[0]


async def _reads(session: AsyncSession, runs: list[AutomationRun]) -> list[RunRead]:
    """Project runs with their ordered action outcomes (codes and references only)."""
    if not runs:
        return []
    actions = (await session.scalars(select(AutomationRunAction).where(
        AutomationRunAction.run_id.in_([r.id for r in runs])).order_by(
        AutomationRunAction.run_id, AutomationRunAction.ordinal))).all()
    by_run: dict[UUID, list[RunActionRead]] = {}
    for a in actions:
        by_run.setdefault(a.run_id, []).append(RunActionRead(
            ordinal=a.ordinal, type=a.action_type, status=a.status, attempts=a.attempts,
            error_code=a.error_code, result_reference=a.result_reference,
            approval_expires_at=a.approval_expires_at))
    return [RunRead(
        id=r.id, automation_id=r.automation_id, revision=r.revision, trigger_type=r.trigger_type,
        trigger_event_id=r.trigger_event_id, scheduled_slot=r.scheduled_slot, depth=r.depth,
        origin_automation_id=r.origin_automation_id, origin_run_id=r.origin_run_id, status=r.status,
        reason=r.reason, attempts=r.attempts, created_at=r.created_at, finished_at=r.finished_at,
        actions=by_run.get(r.id, [])) for r in runs]


async def list_runs(session: AsyncSession, owner_id: int, automation_id: UUID, limit: int = 50) -> RunPage:
    """Newest-first run history for a rule, retained after pause or delete (bounded to 100)."""
    exists = await session.scalar(select(Automation.id).where(
        Automation.id == automation_id, Automation.owner_id == owner_id))
    if exists is None:
        raise RunMissing
    runs = (await session.scalars(select(AutomationRun).where(
        AutomationRun.automation_id == automation_id, AutomationRun.owner_id == owner_id
    ).order_by(AutomationRun.created_at.desc()).limit(min(max(limit, 1), 100)))).all()
    return RunPage(items=await _reads(session, list(runs)))


def _approval_hash(run: AutomationRun, ordinal: int, spec: Mapping[str, Any], destination: str | None) -> str:
    """Bind an approval to the run, slot, immutable revision, exact action and webhook destination digest."""
    blob = json.dumps(
        [str(run.id), ordinal, run.revision, dict(spec), destination], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


async def _fence_current(session: AsyncSession, run: AutomationRun, *, share: bool = False) -> bool:
    """True while the rule is live, enabled and still at the run's revision.

    ``share=True`` takes ``FOR SHARE`` on the head so an edit (which locks ``FOR UPDATE``) waits for
    the current action transaction; in-database effects therefore commit before any newer revision.
    """
    stmt = select(Automation).where(Automation.id == run.automation_id)
    head = await session.scalar(stmt.with_for_update(read=True) if share else stmt)
    return (head is not None and head.deleted_at is None and head.enabled and head.revision == run.revision
            and head.owner_id == run.owner_id)


def _finish(run: AutomationRun, status: str, reason: str | None) -> None:
    """Set a terminal run status; the caller also skips pending actions via ``_skip_pending``."""
    run.status, run.reason, run.finished_at = status, reason, datetime.now(UTC)


async def _skip_pending(session: AsyncSession, run_id: UUID, code: str) -> None:
    """Mark every not-yet-started action of a finished run skipped so history is explicit."""
    rows = await session.scalars(select(AutomationRunAction).where(
        AutomationRunAction.run_id == run_id, AutomationRunAction.status.in_(("pending", "approved", "awaiting_approval"))))
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
    session: AsyncSession, owner_id: int, session_hash: str, run_id: UUID, ordinal: int, approve: bool,
    settings: Settings,
) -> RunRead:
    """Record the owner's decision on one action waiting for approval.

    Approval is bound to the action definition of the cited revision (``approval_hash``), expires
    after 24 hours and is void if the rule changed, was paused or deleted (revision fence). On
    approve the owner session digest is kept only until the worker performs the action. Deny
    ends the run. Nothing is executed here; the worker does that after the commit.

    Raises:
        RunMissing: Unknown run or action.
        RunConflict: Not waiting, expired or fenced out.
    """
    run = await session.scalar(select(AutomationRun).where(
        AutomationRun.id == run_id, AutomationRun.owner_id == owner_id).with_for_update())
    row = None if run is None else await session.scalar(select(AutomationRunAction).where(
        AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
    if run is None or row is None:
        raise RunMissing
    if run.status != "awaiting_approval" or row.status != "awaiting_approval":
        raise RunConflict("not_pending", "Action is not waiting for approval")
    now = datetime.now(UTC)
    rev = await session.scalar(select(AutomationRevision).where(
        AutomationRevision.automation_id == run.automation_id, AutomationRevision.revision == run.revision))
    spec = rev.actions[ordinal - 1] if rev is not None and ordinal <= len(rev.actions) else None
    problem = None
    if row.approval_expires_at is None or row.approval_expires_at <= now:
        problem = ("approval_expired", "Approval expired")
    elif spec is None or row.approval_hash != _approval_hash(run, ordinal, spec, row.destination_revision):
        problem = ("approval_mismatch", "Approval no longer matches the action")
    elif not await _fence_current(session, run):
        problem = ("stale_revision", "Rule changed after this run was queued")
    elif _destination_stale(settings, spec, row.destination_revision):
        problem = ("stale_destination", "Webhook destination changed after this approval was requested")
    elif dependencies_missing(rev):
        problem = ("dependency_unavailable", "A required module is unavailable")
    if problem is not None:
        dropped = problem[0] in ("stale_revision", "stale_destination", "dependency_unavailable")
        row.status, row.error_code = ("skipped" if dropped else "failed"), problem[0]
        _finish(run, "dropped" if dropped else "failed", problem[0])
        await _skip_pending(session, run.id, problem[0])
        await session.commit()
        raise RunConflict(problem[0], problem[1])
    if approve:
        row.status, row.approved_session_hash = "approved", session_hash
        run.status, run.next_attempt_at, run.dispatched_at = "queued", now, None
    else:
        row.status, row.error_code = "denied", "denied_by_owner"
        _finish(run, "failed", "denied")
        await _skip_pending(session, run.id, "denied")
    await session.commit()
    return (await _reads(session, [run]))[0]


async def _mark(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, ordinal: int, status: str,
    code: str | None = None, reference: str | None = None,
) -> None:
    """Persist one action outcome in its own short transaction (also clears any session digest)."""
    async with factory() as session:
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        if row is not None:
            row.status, row.error_code, row.result_reference = status, code, reference
            row.approved_session_hash = None
            await session.commit()


async def _action_step(ctx: dict[str, Any], run_id: UUID, ordinal: int) -> str:
    """Advance one action and return ``succeeded|failed|dropped|awaiting_approval|requires_review|retry``.

    Resumable: every row state is handled, so calling this after a crash converges. The fence is
    checked before every start; ``in_flight`` found here means a previous attempt may have written
    and is converted to review-only, never re-run.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        rev = await session.scalar(select(AutomationRevision).where(
            AutomationRevision.automation_id == run.automation_id, AutomationRevision.revision == run.revision))
        spec = rev.actions[ordinal - 1]
        state = row.status
        if state == "succeeded":
            return "succeeded"
        if state in ("failed", "denied", "skipped"):
            return "failed"
        # run_agent is idempotent (client_request_id), so an interrupted one is simply attempted again.
        if state == "requires_review" or (state == "in_flight" and spec["type"] != "run_agent"):
            row.status, row.error_code = "requires_review", row.error_code or "ambiguous_after_restart"
            await session.commit()
            return "requires_review"
        if state == "awaiting_approval":
            return "awaiting_approval"
        settings = cast(Settings, ctx["settings"])
        kind = spec["type"]
        stale = None
        if not await _fence_current(session, run, share=True):
            stale = "stale_revision"
        elif dependencies_missing(rev):
            stale = "dependency_unavailable"
        elif state == "approved" and _destination_stale(settings, spec, row.destination_revision):
            stale = "stale_destination"
        if stale is not None:
            row.status, row.error_code = "skipped", stale
            await session.commit()
            return "dropped"
        if kind in APPROVAL_ACTIONS and state == "pending":
            try:
                destination = webhook_profile_revision(settings, spec["alias"]) if kind == "call_webhook" else None
            except ValueError:
                destination = None
            if kind == "call_webhook" and destination is None:
                row.status, row.error_code = "failed", "webhook_unavailable"
                await session.commit()
                return "failed"
            row.destination_revision = destination
            row.status, row.approval_hash = "awaiting_approval", _approval_hash(run, ordinal, spec, destination)
            row.approval_expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours)
            await emit(session, run.owner_id, NotificationEmit(
                dedupe_key=f"automation-approval:{run.id}:{ordinal}", kind="automation.approval",
                title=rev.name[:300], body="An automation action is waiting for your approval."))
            await session.commit()
            return "awaiting_approval"
        row.attempts += 1
        if kind in ("create_notification", "create_task"):
            return await _in_database_action(session, run, row, rev, spec, factory)
        if kind == "generate_brief":
            await session.commit()  # release locks; brief generation commits internally
            return await _generate_brief(ctx, run, row)
        session_hash, destination = row.approved_session_hash, row.destination_revision
        if kind == "call_webhook":
            row.approved_session_hash = None  # the webhook path needs no owner session
        # run_agent keeps the digest until success: its idempotent start may be re-attempted.
        row.status = "in_flight"  # point of no return: committed before the external call
        await session.commit()
    if kind == "run_agent":
        return await _start_agent(ctx, run, ordinal, spec, session_hash, rev.name)
    return await _send_webhook(ctx, run, ordinal, spec, factory, destination)


async def _in_database_action(
    session: AsyncSession, run: AutomationRun, row: AutomationRunAction, rev: AutomationRevision,
    spec: Mapping[str, Any], factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a notification or task and flip the ledger row in one transaction (exactly-once)."""
    try:
        if spec["type"] == "create_notification":
            # The dedupe key makes even a repeated insert harmless.
            await emit(session, run.owner_id, NotificationEmit(
                dedupe_key=f"automation:{run.id}:{row.ordinal}", kind="automation.rule", title=rev.name[:300],
                body=spec["message"], link=spec.get("link")))
            reference = f"notification:{run.id}:{row.ordinal}"
        else:
            days = spec.get("due_in_days")
            task = await create_task_in_uow(session, run.owner_id, TaskCreate(
                title=spec["title"], description=spec.get("description"),
                due_date=(datetime.now(UTC) + timedelta(days=days)).date() if days is not None else None))
            reference = f"task:{task.id}"
        row.status, row.error_code, row.result_reference = "succeeded", None, reference
        await session.commit()
        return "succeeded"
    except (TaskConflict, ValidationError, ValueError):
        await session.rollback()
        await _mark(factory, run.id, row.ordinal, "failed", "rejected_by_owner_module")
        return "failed"
    except Exception:
        await session.rollback()
        return await _transient(factory, run.id, row.ordinal)


async def _transient(factory: async_sessionmaker[AsyncSession], run_id: UUID, ordinal: int) -> str:
    """Retry a retry-safe action with bounded attempts; exhausted attempts fail the action.

    ``attempts`` was already incremented when the step started, so it counts the tries made.
    """
    async with factory() as session:
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        if row.attempts >= MAX_ACTION_ATTEMPTS:
            row.status, row.error_code = "failed", "retries_exhausted"
            await session.commit()
            return "failed"
        await session.commit()
        return "retry"


async def _generate_brief(ctx: dict[str, Any], run: AutomationRun, row: AutomationRunAction) -> str:
    """Generate the daily brief via the dashboard public API; safe to retry (force=False dedupes)."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    try:
        async with factory() as session:
            schedule = await dashboard.read_schedule(session, run.owner_id)
            day = datetime.now(UTC).astimezone(ZoneInfo(schedule.timezone)).date()
            brief = await dashboard.generate_brief(
                session, run.owner_id, day, schedule.timezone, settings=cast(Settings, ctx["settings"]),
                redis=cast(Redis, ctx["redis"]), force=False)
        await _mark(factory, run.id, row.ordinal, "succeeded", None, f"brief:{getattr(brief, 'id', 'daily')}")
        return "succeeded"
    except dashboard.BriefEmpty:
        await _mark(factory, run.id, row.ordinal, "succeeded", None, "brief:empty")
        return "succeeded"
    except dashboard.BriefUnavailable:
        return await _transient(factory, run.id, row.ordinal)
    except Exception:
        return await _transient(factory, run.id, row.ordinal)


async def _automation_conversation(session: AsyncSession, automation_id: UUID, name: str) -> UUID:
    """Return the rule's dedicated Chat conversation, creating it once (same model the Chat route creates).

    P07 profile runs require a live Chat link; this per-rule thread is where the owner opens the
    run, its activity and any approval the agent itself raises.
    """
    existing = await session.scalar(select(Conversation.id).where(
        Conversation.context_kind == "automation", Conversation.context_resource_id == automation_id,
        Conversation.archived.is_(False)).limit(1))
    if existing is not None:
        return existing
    conversation = Conversation(
        title=f"Automation: {name}"[:255], context_kind="automation", context_resource_id=automation_id,
        metadata_json={})
    session.add(conversation)
    await session.flush()
    return conversation.id


async def _start_agent(
    ctx: dict[str, Any], run: AutomationRun, ordinal: int, spec: Mapping[str, Any], session_hash: str | None,
    rule_name: str,
) -> str:
    """Start the approved P07 profile run through ``create_profile_run_in_uow`` (row is already in_flight).

    The run uses the rule's ``profile_id`` at its current revision in the rule's Automation Chat
    conversation, so P07 approvals inside it work. ``client_request_id`` derives from
    (run, ordinal) and the owner-session scope, so a retry after a crash returns the same agent run
    rather than a second one; that is why this action is safe to resume. A rejection raised before
    the run row is written is a clean failure.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    registry = ctx.get("agent_tool_registry")
    if session_hash is None or registry is None:
        await _mark(factory, run.id, ordinal, "failed", "agent_unavailable")
        return "failed"
    try:
        async with factory() as session:
            config = await settings_public.get_ai_execution_config(
                session, cast(Settings, ctx["settings"]), cast(Redis, ctx["redis"]))
            profile_id = spec["profile_id"]
            revision = await agents.current_profile_revision(session, run.owner_id, profile_id, registry, config)
            conversation_id = await _automation_conversation(session, run.automation_id, rule_name)
            started = await agents.create_profile_run_in_uow(
                session, run.owner_id, session_hash, profile_id,
                ProfileRunStart(
                    prompt=spec["instruction"], expected_profile_revision=revision,
                    conversation_id=conversation_id, client_request_id=str(uuid5(run.id, f"action:{ordinal}"))),
                registry, config)
            await session.commit()
        try:  # PostgreSQL is the queue; a lost push is replayed by the agent reconciler.
            await cast(ArqRedis, ctx["redis"]).enqueue_job(
                "process_agent_run", str(started.id), 1, _job_id=f"agent-run:{started.id}:1")
        except Exception:
            pass
    except asyncio.CancelledError:
        raise  # stays in_flight; the idempotent start is attempted again on resume
    except HTTPException:
        await _mark(factory, run.id, ordinal, "failed", "agent_rejected")
        return "failed"
    except Exception:
        return await _transient(factory, run.id, ordinal)
    await _mark(factory, run.id, ordinal, "succeeded", None, f"agent_run:{started.id}")
    return "succeeded"


async def _send_webhook(
    ctx: dict[str, Any], run: AutomationRun, ordinal: int, spec: Mapping[str, Any],
    factory: async_sessionmaker[AsyncSession], destination: str | None,
) -> str:
    """Send the approved webhook once through the shared SSRF-safe transport (row is already in_flight).

    The body is metadata only (rule, run, revision, depth). Origin and depth go out as headers so a
    receiver that calls back into an inbound hook continues the same causal chain. The revision
    fence runs again after DNS and just before the socket write; a non-2xx or timeout after the
    write began is ``requires_review`` and is never replayed.
    """
    settings = cast(Settings, ctx["settings"])

    async def still_current() -> bool:
        """Re-check the rule fence and the approved destination digest immediately before the write."""
        async with factory() as session:
            return await _fence_current(session, run) and not _destination_stale(settings, spec, destination)

    body = {
        "event": spec["event"], "automation_id": str(run.automation_id), "run_id": str(run.id),
        "revision": run.revision, "trigger": run.trigger_type, "depth": run.depth,
    }
    try:
        outcome = await send_webhook_once(
            settings, spec["alias"], body,
            idempotency_key=str(uuid5(run.id, f"action:{ordinal}")),
            headers={"X-Umwelt-Automation-Id": str(run.automation_id), "X-Umwelt-Automation-Origin": str(run.id),
                     "X-Umwelt-Automation-Depth": str(run.depth)},
            before_send=still_current)
    except asyncio.CancelledError:
        await asyncio.shield(_mark(factory, run.id, ordinal, "requires_review", "cancelled_in_flight"))
        raise
    if outcome == "succeeded":
        await _mark(factory, run.id, ordinal, "succeeded", None, f"webhook:{run.id}:{ordinal}")
        return "succeeded"
    if outcome == "unsent":
        async with factory() as session:
            current = await _fence_current(session, run)
        if not current or _destination_stale(settings, spec, destination):
            await _mark(factory, run.id, ordinal, "skipped", "stale_revision" if not current else "stale_destination")
            return "dropped"
        await _mark(factory, run.id, ordinal, "failed", "webhook_unsent")
        return "failed"
    await _mark(factory, run.id, ordinal, "requires_review", "outcome_unknown", f"webhook:{run.id}:{ordinal}")
    return "requires_review"


async def _settle(factory: async_sessionmaker[AsyncSession], run_id: UUID, outcome: str) -> str:
    """Move the run to the status implied by the last action outcome and persist it."""
    async with factory() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        if outcome == "awaiting_approval":
            run.status = "awaiting_approval"
            run.attempts = max(run.attempts - 1, 0)  # waiting for the owner is not a failed pass
        elif outcome == "retry":
            run.status = "queued"
            run.dispatched_at = None
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=15 * 2 ** min(run.attempts, 5))
        else:
            status = {"succeeded": "succeeded", "failed": "failed", "dropped": "dropped",
                      "requires_review": "requires_review"}[outcome]
            _finish(run, status, None if outcome == "succeeded" else outcome)
            if outcome != "succeeded":
                await _skip_pending(session, run_id, "prior_action_" + outcome)
        await session.commit()
        return run.status


async def _heartbeat(factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
    """Refresh ``updated_at`` so a long multi-action run is not mistaken for a dead worker."""
    async with factory() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        run.updated_at = datetime.now(UTC)
        await session.commit()


async def process_run(ctx: dict[str, Any], run_id: str) -> str:
    """Execute (or resume) one queued run: claim it, then run its actions strictly in order.

    Concurrency per rule is 1: a run is claimed only when no sibling run of the same rule is
    ``running``. Each pass counts an attempt (restarts included) so a poisoned run ends as failed
    instead of cycling. The loop stops at the first action that is not ``succeeded``.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    rid, now = UUID(run_id), datetime.now(UTC)
    async with factory() as session:
        run = await session.scalar(select(AutomationRun).where(AutomationRun.id == rid).with_for_update(skip_locked=True))
        if run is None or run.status != "queued" or run.next_attempt_at > now:
            return "noop"
        busy = await session.scalar(select(func.count()).select_from(AutomationRun).where(
            AutomationRun.automation_id == run.automation_id, AutomationRun.id != rid,
            AutomationRun.status == "running", AutomationRun.updated_at > now - RUNNING_STALE_AFTER))
        if busy:
            run.next_attempt_at, run.dispatched_at = now + timedelta(seconds=5), None
            await session.commit()
            return "busy"
        if run.attempts >= MAX_RUN_ATTEMPTS:
            _finish(run, "failed", "attempts_exhausted")
            await _skip_pending(session, rid, "attempts_exhausted")
            await session.commit()
            return "failed"
        run.status, run.attempts = "running", run.attempts + 1
        total = await session.scalar(select(func.count()).select_from(AutomationRunAction).where(
            AutomationRunAction.run_id == rid))
        await session.commit()
    for ordinal in range(1, (total or 0) + 1):
        await _heartbeat(factory, rid)
        outcome = await _action_step(ctx, rid, ordinal)
        if outcome != "succeeded":
            return await _settle(factory, rid, outcome)
    return await _settle(factory, rid, "succeeded")


async def dispatch_runs(factory: async_sessionmaker[AsyncSession], redis: ArqRedis) -> int:
    """Durably (re)enqueue ARQ jobs for runnable runs and expire stale approvals.

    PostgreSQL is the queue authority: a ``queued`` run not dispatched recently, or a ``running``
    run whose worker stopped updating it, is requeued. The ARQ job id carries the dispatch
    generation so a re-dispatch is never swallowed by a finished job of the same run. Returns the
    number of jobs enqueued.
    """
    now = datetime.now(UTC)
    enqueued = 0
    async with factory() as session:
        expired = (await session.scalars(select(AutomationRunAction).where(
            AutomationRunAction.status == "awaiting_approval", AutomationRunAction.approval_expires_at < now
        ).limit(50).with_for_update(skip_locked=True))).all()
        for row in expired:
            row.status, row.error_code = "failed", "approval_expired"
            run = await session.get(AutomationRun, row.run_id, with_for_update=True)
            _finish(run, "failed", "approval_expired")
            await _skip_pending(session, run.id, "approval_expired")
        runs = (await session.scalars(select(AutomationRun).where(
            ((AutomationRun.status == "queued") & (AutomationRun.next_attempt_at <= now)
             & (AutomationRun.dispatched_at.is_(None) | (AutomationRun.dispatched_at < now - DISPATCH_STALE_AFTER)))
            | ((AutomationRun.status == "running") & (AutomationRun.updated_at < now - RUNNING_STALE_AFTER))
        ).order_by(AutomationRun.created_at).limit(50).with_for_update(skip_locked=True))).all()
        for run in runs:
            run.status = "queued"  # a stale running run resumes; in_flight rows turn review-only there
            run.dispatch_generation += 1
            run.dispatched_at = now
            await redis.enqueue_job(
                "process_automation_run", str(run.id), _job_id=f"automation-run:{run.id}:{run.dispatch_generation}",
                _defer_until=now)
            enqueued += 1
        await session.commit()
    return enqueued
