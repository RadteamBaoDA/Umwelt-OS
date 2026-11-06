"""Automation run planning: loop guard, trigger enqueue, dependency checks and run admission.

Identity and dedupe: a run is keyed by ``(automation_id, revision, trigger_key)``; the unique index is the only dedupe
fence and every producer inserts with ON CONFLICT DO NOTHING. Revision fence: a run cites an immutable revision and
"revision differs" is the single drop condition. Loop bounds: depth above ``MAX_DEPTH``, self-reaction, known loop
pairs, cooldown and the hourly cap end in a recorded ``skipped`` run instead of work. The full invariant list lives in
``modules.automations.execution``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.workspaces.schemas import Scope
from modules.automations._execution_evidence import (
    _retained_document_evidence_current,
)
from modules.automations._execution_types import (
    ACTION_MODULE,
    COOLDOWN_SECONDS,
    MAX_ACTIONS,
    MAX_DEPTH,
    MAX_RUNS_PER_HOUR,
    TRIGGER_MODULE,
    _modules_cache,
)
from modules.automations.conditions import TRIGGER_FIELDS, evaluate, validate_sample
from modules.automations.models import (
    Automation,
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
    AutomationTrigger,
)
from modules.automations.scope import _actor, _admit, _require_owner
from modules.settings import public as settings_public


def loop_guard(trigger: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> str | None:
    """Name a known self-feeding trigger/action pair, or None.

    Pairs: ``task_due`` + ``create_task`` whose new task is already inside the lead window
    (re-fires every cycle); ``entity_changed`` + ``run_agent`` (the agent can write entities);
    ``webhook`` + ``call_webhook`` (the outbound endpoint can post back to the inbound hook).
    """
    kinds = {a["type"] for a in actions}
    kind = trigger["type"]
    if kind == "task_due" and any(
        a["type"] == "create_task"
        and a.get("due_in_days") is not None
        and a["due_in_days"] * 1440 <= trigger.get("lead_minutes", 0)
        for a in actions
    ):
        return "loop_task_due_create_task"
    if kind == "entity_changed" and "run_agent" in kinds:
        return "loop_entity_changed_run_agent"
    if kind == "webhook" and "call_webhook" in kinds:
        return "loop_webhook_call_webhook"
    return None


async def enqueue_trigger(
    session: AsyncSession,
    trigger_type: str,
    event_key: str,
    payload: Mapping[str, Any],
    *,
    hook: str | None = None,
    origin_automation_id: UUID | None = None,
    origin_run_id: UUID | None = None,
    depth: int = 0,
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Offer one trigger event to the automation inbox inside the producer's transaction.

    ``event_key`` must be the producer's stable event id; re-offering it is a no-op (returns False),
    which is the first dedupe layer. ``payload`` carries only metadata fields declared for the
    trigger (see ``TRIGGER_FIELDS``) and never content. Exact new-document provenance is kept in
    private sidecars outside this condition payload and is revalidated against the ready-event receipt,
    Documents and Source retention fence. Nothing is committed here.

    Raises:
        ValueError: Unknown trigger, bad key, undeclared payload field, missing hook or invalid provenance.
    """
    _require_owner(scope)
    if trigger_type not in TRIGGER_FIELDS or trigger_type == "schedule":
        raise ValueError("trigger type cannot be offered by a producer")
    if not 1 <= len(event_key) <= 200 or not 0 <= depth <= 50:
        raise ValueError("invalid event key or depth")
    if trigger_type == "new_document":
        if document_id is None or document_version_id is None:
            raise ValueError("new-document triggers require exact private evidence provenance")
        try:
            event_id = UUID(event_key)
        except (TypeError, ValueError):
            raise ValueError("new-document event key must be its canonical outbox UUID") from None
        if str(event_id) != event_key:
            raise ValueError("new-document event key must be its canonical outbox UUID")
        # Non-locking proof: the producer's transaction already owns the access locks.
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if not await _retained_document_evidence_current(
            session,
            payload,
            event_id=event_id,
            document_id=document_id,
            document_version_id=document_version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        ):
            raise ValueError("new-document provenance no longer matches retained evidence")
    elif document_id is not None or document_version_id is not None:
        raise ValueError("Document provenance is valid only for new-document triggers")
    validate_sample(trigger_type, dict(payload))
    stored = dict(payload)
    if trigger_type == "webhook":
        if not hook:
            raise ValueError("webhook triggers need a hook name")
        stored["hook"] = hook
    result = await session.execute(
        insert(AutomationTrigger)
        .values(
            id=uuid7(),
            workspace_id=scope.workspace_id,
            owner_id=_actor(scope),
            trigger_type=trigger_type,
            event_key=event_key,
            payload=stored,
            document_id=document_id,
            document_version_id=document_version_id,
            depth=depth,
            origin_automation_id=origin_automation_id,
            origin_run_id=origin_run_id,
            status="pending",
        )
        .on_conflict_do_nothing(constraint="uq_automation_triggers_event")
        .returning(AutomationTrigger.id)
    )
    return result.scalar_one_or_none() is not None


def dependencies_missing(rev: AutomationRevision) -> list[str]:
    """Return build-time descriptor gaps used while planning and validating owner decisions.

    Effect admission uses ``_action_modules_enabled`` so persisted owner disables are refreshed
    for the exact action immediately before execution.
    """
    if "registry" not in _modules_cache:
        from core.modules import register_modules  # lazy: core.modules imports module descriptors

        _modules_cache["registry"] = register_modules()
    registry = _modules_cache["registry"]
    needed = {TRIGGER_MODULE[rev.trigger["type"]]} | {ACTION_MODULE[a["type"]] for a in rev.actions}
    return sorted(
        m for m in needed if m is not None and (m not in registry or not registry[m].enabled)
    )


async def _action_modules_enabled(
    session: AsyncSession,
    action_type: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Read persisted automation and exact action-owner availability at effect admission."""
    if not await settings_public.module_is_enabled(
        session,
        "automations",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return False
    target = ACTION_MODULE[action_type]
    return target is None or await settings_public.module_is_enabled(
        session,
        target,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def origin_for_reference(
    session: AsyncSession,
    reference: str,
    *,
    scope: Scope,
) -> tuple[UUID, UUID, int] | None:
    """Map an effect reference such as ``task:<id>`` to ``(automation_id, run_id, depth)`` of its creator.

    Producers call this before offering a trigger so an event caused by an automation continues
    that run's causal chain (depth + 1, origin ids) instead of looking like a root event.
    """
    row = (
        await session.execute(
            select(AutomationRun.automation_id, AutomationRun.id, AutomationRun.depth)
            .join(AutomationRunAction, AutomationRunAction.run_id == AutomationRun.id)
            .where(
                AutomationRun.workspace_id == scope.workspace_id,
                AutomationRun.owner_id == _actor(scope),
                AutomationRunAction.result_reference == reference,
                AutomationRunAction.status == "succeeded",
            )
            .limit(1)
        )
    ).first()
    return None if row is None else (row[0], row[1], row[2])


async def _admission(
    session: AsyncSession,
    rev: AutomationRevision,
    trigger_type: str,
    depth: int,
    origin_automation_id: UUID | None,
    now: datetime,
    *,
    scope: Scope,
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
    base = (
        AutomationRun.workspace_id == scope.workspace_id,
        AutomationRun.owner_id == _actor(scope),
        AutomationRun.automation_id == rev.automation_id,
        AutomationRun.status != "skipped",
    )
    hourly = await session.scalar(
        select(func.count())
        .select_from(AutomationRun)
        .where(*base, AutomationRun.created_at > now - timedelta(hours=1))
    )
    if (hourly or 0) >= MAX_RUNS_PER_HOUR:
        return "rate_limited", now
    if trigger_type == "schedule":
        return None, now
    last = await session.scalar(select(func.max(AutomationRun.next_attempt_at)).where(*base))
    run_at = last + timedelta(seconds=COOLDOWN_SECONDS) if last is not None else now
    return None, max(run_at, now)


async def _document_trigger_admissible(
    session: AsyncSession,
    *,
    trigger_type: str,
    trigger_key: str,
    trigger_event_id: str | None,
    payload: Mapping[str, Any],
    document_id: UUID | None,
    document_version_id: UUID | None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Decide whether the Document provenance of a trigger permits planning a run.

    For ``new_document`` triggers the event token must be a canonical UUID matching both the trigger key and
    event id, and the retained Document evidence must still be current under a freshly admitted access fence;
    any miss returns False (no run). Other trigger types must carry no Document provenance, else ``ValueError``.
    """
    if trigger_type == "new_document":
        if document_id is None or document_version_id is None:
            return False
        event_token = trigger_key.removeprefix("event:")
        try:
            event_id = UUID(event_token)
        except (TypeError, ValueError):
            return False
        if trigger_key != f"event:{event_id}" or trigger_event_id != str(event_id):
            return False
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if not await _retained_document_evidence_current(
            session,
            payload,
            event_id=event_id,
            document_id=document_id,
            document_version_id=document_version_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=fence,
        ):
            return False
    elif document_id is not None or document_version_id is not None:
        raise ValueError("Document provenance is valid only for new-document runs")
    return True


async def plan_run(
    session: AsyncSession,
    *,
    rev: AutomationRevision,
    trigger_type: str,
    trigger_key: str,
    trigger_event_id: str | None,
    slot: datetime | None,
    payload: Mapping[str, Any],
    depth: int,
    origin_automation_id: UUID | None,
    origin_run_id: UUID | None,
    apply_conditions: bool = True,
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Create the queued (or recorded-skipped) run for one trigger, or None when nothing is created.

    None means conditions did not match or this identity already exists (duplicate event, retry or
    double slot or exact Document evidence was unavailable). Document sidecars stay outside the
    condition payload. The insert is ON CONFLICT DO NOTHING on ``uq_automation_runs_identity`` so
    two concurrent planners cannot both succeed. Action rows are created up front (<= 10) so every
    outcome has a durable slot. The caller commits.
    """
    _require_owner(scope)
    if not await _document_trigger_admissible(
        session,
        trigger_type=trigger_type,
        trigger_key=trigger_key,
        trigger_event_id=trigger_event_id,
        payload=payload,
        document_id=document_id,
        document_version_id=document_version_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return None
    if apply_conditions and rev.conditions and not evaluate(rev.conditions, dict(payload))[0]:
        return None
    now = datetime.now(UTC)
    reason, run_at = await _admission(
        session, rev, trigger_type, depth, origin_automation_id, now, scope=scope
    )
    run_id = uuid7()
    result = await session.execute(
        insert(AutomationRun)
        .values(
            id=run_id,
            workspace_id=scope.workspace_id,
            owner_id=_actor(scope),
            automation_id=rev.automation_id,
            revision=rev.revision,
            trigger_type=trigger_type,
            trigger_key=trigger_key,
            trigger_event_id=trigger_event_id,
            scheduled_slot=slot,
            depth=min(depth, 50),
            origin_automation_id=origin_automation_id,
            origin_run_id=origin_run_id,
            status="skipped" if reason else "queued",
            reason=reason,
            payload=dict(payload),
            document_id=document_id,
            document_version_id=document_version_id,
            document_evidence_revoked=False,
            attempts=0,
            dispatch_generation=0,
            next_attempt_at=run_at,
            finished_at=now if reason else None,
        )
        .on_conflict_do_nothing(constraint="uq_automation_runs_identity")
        .returning(AutomationRun.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    if not reason:
        session.add_all(
            AutomationRunAction(
                id=uuid7(),
                run_id=run_id,
                ordinal=i,
                action_type=a["type"],
                status="pending",
                attempts=0,
            )
            for i, a in enumerate(rev.actions[:MAX_ACTIONS], start=1)
        )
    return run_id


async def live_rules(
    session: AsyncSession, trigger_type: str, *, scope: Scope
) -> list[AutomationRevision]:
    """Current snapshots of live, enabled rules for one trigger type (bounded to 100)."""
    rows = await session.execute(
        select(AutomationRevision)
        .join(
            Automation,
            (AutomationRevision.automation_id == Automation.id)
            & (AutomationRevision.revision == Automation.revision),
        )
        .where(
            Automation.workspace_id == scope.workspace_id,
            Automation.owner_id == _actor(scope),
            Automation.deleted_at.is_(None),
            Automation.enabled.is_(True),
            AutomationRevision.trigger["type"].astext == trigger_type,
        )
        .limit(100)
    )
    return list(rows.scalars().all())


async def run_exists(
    session: AsyncSession, rev: AutomationRevision, trigger_key: str, *, scope: Scope
) -> bool:
    """Cheap pre-check so sweeps do not re-plan identities that already have a run (dedupe stays the index)."""
    return (
        await session.scalar(
            select(AutomationRun.id)
            .where(
                AutomationRun.workspace_id == scope.workspace_id,
                AutomationRun.owner_id == _actor(scope),
                AutomationRun.automation_id == rev.automation_id,
                AutomationRun.revision == rev.revision,
                AutomationRun.trigger_key == trigger_key,
            )
            .limit(1)
        )
        is not None
    )
