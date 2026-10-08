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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.agents import public as agents
from modules.agents.public import ProfileRunStart
from modules.automations.conditions import TRIGGER_FIELDS, evaluate, validate_sample
from modules.automations.models import (
    Automation,
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
    AutomationTrigger,
)
from modules.automations.schemas import RunActionRead, RunPage, RunRead
from modules.automations.scope import _actor, _admit, _require_owner
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


@dataclass(frozen=True)
class AutomationCleanupProgress:
    """Return bounded cleanup progress keyed by operation and stable owner-row classifications.

    Provisional unavailable IDs may be resolved by later reference pages; only IDs in
    ``unavailable_ids`` are terminal for the caller's operation. No copied payload is exposed.
    """

    next_cursor: UUID | None
    complete: bool
    changed_count: int
    operation_id: UUID
    provisional_unavailable_ids: tuple[UUID, ...] = ()
    unavailable_ids: tuple[UUID, ...] = ()


class RunMissing(Exception):
    """Raised for an absent or foreign-owned run or rule."""


class RunConflict(Exception):
    """Raised when a request no longer matches the run, action or rule state."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep a stable machine code and optional current revision for HTTP 409."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


def loop_guard(trigger: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> str | None:
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
    session: AsyncSession, trigger_type: str, event_key: str, payload: Mapping[str, Any],
    *, hook: str | None = None, origin_automation_id: UUID | None = None, origin_run_id: UUID | None = None,
    depth: int = 0, document_id: UUID | None = None, document_version_id: UUID | None = None,
    scope: Scope, multi_workspace_enabled: bool,
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
            session, payload, event_id=event_id, document_id=document_id,
            document_version_id=document_version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
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
        insert(AutomationTrigger).values(
            id=uuid4(), workspace_id=scope.workspace_id, owner_id=_actor(scope), trigger_type=trigger_type,
            event_key=event_key, payload=stored,
            document_id=document_id, document_version_id=document_version_id,
            depth=depth, origin_automation_id=origin_automation_id, origin_run_id=origin_run_id, status="pending")
        .on_conflict_do_nothing(constraint="uq_automation_triggers_event").returning(AutomationTrigger.id))
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
    return sorted(m for m in needed if m is not None and (m not in registry or not registry[m].enabled))


async def _action_modules_enabled(
    session: AsyncSession, action_type: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Read persisted automation and exact action-owner availability at effect admission."""
    if not await settings_public.module_is_enabled(
        session, "automations", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        return False
    target = ACTION_MODULE[action_type]
    return target is None or await settings_public.module_is_enabled(
        session, target, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def origin_for_reference(
    session: AsyncSession, reference: str, *, scope: Scope,
) -> tuple[UUID, UUID, int] | None:
    """Map an effect reference such as ``task:<id>`` to ``(automation_id, run_id, depth)`` of its creator.

    Producers call this before offering a trigger so an event caused by an automation continues
    that run's causal chain (depth + 1, origin ids) instead of looking like a root event.
    """
    row = (await session.execute(
        select(AutomationRun.automation_id, AutomationRun.id, AutomationRun.depth)
        .join(AutomationRunAction, AutomationRunAction.run_id == AutomationRun.id)
        .where(
            AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
            AutomationRunAction.result_reference == reference, AutomationRunAction.status == "succeeded")
        .limit(1))).first()
    return None if row is None else (row[0], row[1], row[2])


async def _admission(
    session: AsyncSession, rev: AutomationRevision, trigger_type: str, depth: int,
    origin_automation_id: UUID | None, now: datetime, *, scope: Scope,
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
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.automation_id == rev.automation_id, AutomationRun.status != "skipped",
    )
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
    session: AsyncSession, *, rev: AutomationRevision, trigger_type: str, trigger_key: str,
    trigger_event_id: str | None, slot: datetime | None, payload: Mapping[str, Any], depth: int,
    origin_automation_id: UUID | None, origin_run_id: UUID | None, apply_conditions: bool = True,
    document_id: UUID | None = None, document_version_id: UUID | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Create the queued (or recorded-skipped) run for one trigger, or None when nothing is created.

    None means conditions did not match or this identity already exists (duplicate event, retry or
    double slot or exact Document evidence was unavailable). Document sidecars stay outside the
    condition payload. The insert is ON CONFLICT DO NOTHING on ``uq_automation_runs_identity`` so
    two concurrent planners cannot both succeed. Action rows are created up front (<= 10) so every
    outcome has a durable slot. The caller commits.
    """
    _require_owner(scope)
    if trigger_type == "new_document":
        if document_id is None or document_version_id is None:
            return None
        event_token = trigger_key.removeprefix("event:")
        try:
            event_id = UUID(event_token)
        except (TypeError, ValueError):
            return None
        if (trigger_key != f"event:{event_id}" or trigger_event_id != str(event_id)):
            return None
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if not await _retained_document_evidence_current(
            session, payload, event_id=event_id, document_id=document_id,
            document_version_id=document_version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
        ):
            return None
    elif document_id is not None or document_version_id is not None:
        raise ValueError("Document provenance is valid only for new-document runs")
    if apply_conditions and rev.conditions and not evaluate(rev.conditions, dict(payload))[0]:
        return None
    now = datetime.now(UTC)
    reason, run_at = await _admission(session, rev, trigger_type, depth, origin_automation_id, now, scope=scope)
    run_id = uuid4()
    result = await session.execute(
        insert(AutomationRun).values(
            id=run_id, workspace_id=scope.workspace_id, owner_id=_actor(scope),
            automation_id=rev.automation_id, revision=rev.revision,
            trigger_type=trigger_type, trigger_key=trigger_key, trigger_event_id=trigger_event_id,
            scheduled_slot=slot, depth=min(depth, 50), origin_automation_id=origin_automation_id,
            origin_run_id=origin_run_id, status="skipped" if reason else "queued", reason=reason,
            payload=dict(payload), document_id=document_id, document_version_id=document_version_id,
            document_evidence_revoked=False, attempts=0, dispatch_generation=0, next_attempt_at=run_at,
            finished_at=now if reason else None)
        .on_conflict_do_nothing(constraint="uq_automation_runs_identity").returning(AutomationRun.id))
    if result.scalar_one_or_none() is None:
        return None
    if not reason:
        session.add_all(
            AutomationRunAction(id=uuid4(), run_id=run_id, ordinal=i, action_type=a["type"], status="pending", attempts=0)
            for i, a in enumerate(rev.actions[:MAX_ACTIONS], start=1))
    return run_id


async def live_rules(session: AsyncSession, trigger_type: str, *, scope: Scope) -> list[AutomationRevision]:
    """Current snapshots of live, enabled rules for one trigger type (bounded to 100)."""
    rows = await session.execute(
        select(AutomationRevision).join(
            Automation,
            (AutomationRevision.automation_id == Automation.id) & (AutomationRevision.revision == Automation.revision),
        ).where(
            Automation.workspace_id == scope.workspace_id, Automation.owner_id == _actor(scope),
            Automation.deleted_at.is_(None), Automation.enabled.is_(True),
            AutomationRevision.trigger["type"].astext == trigger_type).limit(100))
    return list(rows.scalars().all())


async def run_exists(session: AsyncSession, rev: AutomationRevision, trigger_key: str, *, scope: Scope) -> bool:
    """Cheap pre-check so sweeps do not re-plan identities that already have a run (dedupe stays the index)."""
    return await session.scalar(select(AutomationRun.id).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.automation_id == rev.automation_id, AutomationRun.revision == rev.revision,
        AutomationRun.trigger_key == trigger_key).limit(1)) is not None


async def _retained_document_evidence_current(
    session: AsyncSession, payload: Mapping[str, Any], *, event_id: UUID,
    document_id: UUID, document_version_id: UUID, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Hold the Source lifecycle lock while proving exact retained Document/version ownership.

    Source is locked before re-reading Documents, matching canonical deletion and mutation order.
    Paused and connector-only archived sources remain eligible; unfinished or failed with-data
    purges fail closed. The original ready-event receipt proves historical identity; its generation
    may predate a pause, so current Source generation is captured separately and compared only to
    the current retained-version fence. The caller holds its transaction through publication.
    """
    try:
        source_id = UUID(str(payload.get("source_id")))
    except (TypeError, ValueError):
        return False
    from modules.ingestion import public as ingestion
    from modules.knowledge.documents import public as documents
    from modules.sources import public as sources

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (proof is None or proof.document_id != document_id or proof.document_version_id != document_version_id
            or proof.source_id != source_id):
        return False

    fence = await sources.lock_retained_evidence_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence)
    if fence is None:
        return False
    version_fence = (await documents.review_version_fences(
        session, [document_version_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )).get(document_version_id)
    return (
        version_fence is not None
        and version_fence.document_id == document_id
        and version_fence.source_id == source_id == fence.id
        and version_fence.current_source_generation == fence.generation
    )


async def _trigger_evidence_current(
    session: AsyncSession, event: AutomationTrigger, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Require exact ready-event provenance and a locked retained-Source fence before inbox fan-out."""
    if event.document_evidence_revoked:
        return False
    if (event.document_id is None) != (event.document_version_id is None):
        return False
    try:
        validate_sample("new_document", event.payload)
    except (TypeError, ValueError):
        return False
    try:
        source_id = UUID(str(event.payload.get("source_id")))
    except (ValueError, TypeError):
        return False
    try:
        event_id = UUID(event.event_key)
    except (ValueError, TypeError):
        return False
    if str(event_id) != event.event_key:
        return False
    if event.document_id is not None and event.document_version_id is not None:
        return await _retained_document_evidence_current(
            session, event.payload, event_id=event_id, document_id=event.document_id,
            document_version_id=event.document_version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    from modules.ingestion import public as ingestion

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if proof is None or proof.source_id != source_id:
        return False
    if not await _retained_document_evidence_current(
        session, event.payload, event_id=event_id, document_id=proof.document_id,
        document_version_id=proof.document_version_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    ):
        return False
    event.document_id, event.document_version_id = proof.document_id, proof.document_version_id
    return True


def _scrub_document_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only condition metadata copied from the Document and its Source."""
    return {key: value for key, value in payload.items() if key not in {
        "title", "mime_type", "source_type", "source_id",
    }}


async def _run_evidence_current(
    session: AsyncSession, run: AutomationRun, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence,
) -> bool:
    """Require exact ready-event lineage and a locked retained-Source fence before run action admission."""
    if run.trigger_type != "new_document":
        return True
    if run.document_evidence_revoked or (run.document_id is None) != (run.document_version_id is None):
        return False
    if run.document_id is None or run.document_version_id is None:
        event_id: UUID | None = None
        if run.trigger_event_id is not None:
            try:
                candidate = UUID(run.trigger_event_id)
                if str(candidate) == run.trigger_event_id:
                    event_id = candidate
            except (ValueError, TypeError):
                pass
        if event_id is None and run.trigger_key.startswith("event:"):
            token = run.trigger_key.removeprefix("event:")
            try:
                candidate = UUID(token)
                if str(candidate) == token and run.trigger_key == f"event:{candidate}":
                    event_id = candidate
            except (ValueError, TypeError):
                pass
        if event_id is None or run.trigger_key != f"event:{event_id}" or run.trigger_event_id != str(event_id):
            return False
        from modules.ingestion import public as ingestion

        proof = await ingestion.resolve_ready_event_provenance(
            session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if proof is None:
            return False
        try:
            if UUID(str(run.payload.get("source_id"))) != proof.source_id:
                return False
        except (ValueError, TypeError):
            return False
        if not await _retained_document_evidence_current(
            session, run.payload, event_id=event_id, document_id=proof.document_id,
            document_version_id=proof.document_version_id, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        ):
            return False
        run.document_id, run.document_version_id = proof.document_id, proof.document_version_id
        return True

    event_id = None
    if run.trigger_event_id is not None:
        try:
            candidate = UUID(run.trigger_event_id)
            if str(candidate) == run.trigger_event_id:
                event_id = candidate
        except (TypeError, ValueError):
            pass
    if (event_id is None or run.trigger_key != f"event:{event_id}"
            or run.trigger_event_id != str(event_id)):
        return False
    return await _retained_document_evidence_current(
        session, run.payload, event_id=event_id, document_id=run.document_id,
        document_version_id=run.document_version_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )


async def _legacy_event_matches_cleanup(
    session: AsyncSession, event_id: UUID, *, document_id: UUID, source_id: UUID,
    version_ids: tuple[UUID, ...], scope: Scope, multi_workspace_enabled: bool,
) -> Any | None:
    """Use Ingestion's strict outbox resolver to classify one pre-sidecar event against a receipt."""
    from modules.ingestion import public as ingestion

    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, document_id=document_id, source_id=source_id,
        accepted_version_ids=version_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    return proof


async def _event_belongs_elsewhere(
    session: AsyncSession, event_key: str | None, *, document_id: UUID, scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """True only when a strict canonical ready event provably resolves to a different Document."""
    from modules.ingestion import public as ingestion

    try:
        event_id = UUID(event_key or "")
    except (ValueError, TypeError):
        return False
    if str(event_id) != event_key:
        return False
    # Retained-receipt fallback: an event of an already-deleted other Document is still foreign, not unresolved.
    proof = await ingestion.resolve_ready_event_provenance(
        session, event_id, allow_retained_receipt=True, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return proof is not None and proof.document_id != document_id


async def scrub_document_triggers(
    session: AsyncSession, *, operation_id: UUID, document_id: UUID, source_id: UUID,
    version_ids: tuple[UUID, ...], final_reference_page: bool = False,
    after: UUID | None = None, limit: int = 100, scope: Scope, multi_workspace_enabled: bool,
) -> AutomationCleanupProgress:
    """Scrub one stable keyset page of exact new-document inbox copies without committing.

    Direct rows use private sidecars plus exact ready-event receipt; legacy rows require an exact UUID
    outbox receipt and version in the detached cleanup scope. Unresolved row IDs are provisional until
    the caller marks its last reference page. The caller retains operation and both cursor positions.
    """
    if not 1 <= limit <= 100 or len(version_ids) > 100:
        raise ValueError("Automation trigger cleanup exceeds its page bound")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(AutomationTrigger).where(
        AutomationTrigger.workspace_id == scope.workspace_id, AutomationTrigger.owner_id == _actor(scope),
        AutomationTrigger.trigger_type == "new_document",
    )
    if after is not None:
        statement = statement.where(AutomationTrigger.id > after)
    rows = list((await session.scalars(
        statement.order_by(AutomationTrigger.id).limit(limit).with_for_update()
    )).all())
    complete = len(rows) < limit
    changed = 0
    provisional: list[UUID] = []
    unavailable: list[UUID] = []
    for event in rows:
        if event.document_evidence_revoked:
            continue
        matched = (
            event.document_id == document_id and event.document_version_id is not None
            and event.payload.get("source_id") == str(source_id)
        )
        proven_version = event.document_version_id if matched else None
        if matched:
            try:
                event_id = UUID(event.event_key)
                proof = await _legacy_event_matches_cleanup(
                    session, event_id, document_id=document_id, source_id=source_id, version_ids=version_ids,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                ) if str(event_id) == event.event_key else None
            except (ValueError, TypeError):
                proof = None
            matched = proof is not None and proof.document_version_id == event.document_version_id
        if not matched and event.document_id is None and event.document_version_id is None:
            try:
                event_id = UUID(event.event_key)
                if str(event_id) == event.event_key:
                    proof = await _legacy_event_matches_cleanup(
                        session, event_id, document_id=document_id, source_id=source_id, version_ids=version_ids,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                    )
                    matched = proof is not None
                    proven_version = proof.document_version_id if proof is not None else None
            except (ValueError, TypeError):
                matched = False
        if not matched:
            # Rows proven to belong to another Document (sidecar or resolvable ready event) are skipped;
            # only this Document's unmatched rows and genuinely undecidable legacy rows are reported.
            if event.document_id is not None and event.document_id != document_id:
                continue
            if (event.document_id is None and event.document_version_id is None
                    and await _event_belongs_elsewhere(session, event.event_key, document_id=document_id,
                        scope=scope, multi_workspace_enabled=multi_workspace_enabled)):
                continue
            (unavailable if final_reference_page else provisional).append(event.id)
            continue
        event.document_id = document_id
        event.document_version_id = proven_version
        event.document_evidence_revoked = True
        event.payload = _scrub_document_payload(event.payload)
        event.status = "processed"
        changed += 1
    if changed:
        await session.flush()
    return AutomationCleanupProgress(
        rows[-1].id if rows else after, complete, changed, operation_id, tuple(provisional), tuple(unavailable),
    )


async def scrub_document_runs(
    session: AsyncSession, *, operation_id: UUID, document_id: UUID, source_id: UUID,
    version_ids: tuple[UUID, ...], final_reference_page: bool = False,
    after: UUID | None = None, limit: int = 100, scope: Scope, multi_workspace_enabled: bool,
) -> AutomationCleanupProgress:
    """Scrub one bounded exact run-payload page while retaining action results and effect uncertainty.

    Paged receipts require the caller to retain both evidence-page and run-ID cursor positions.
    """
    if not 1 <= limit <= 100 or len(version_ids) > 100:
        raise ValueError("Automation run cleanup exceeds its page bound")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.trigger_type == "new_document",
    )
    if after is not None:
        statement = statement.where(AutomationRun.id > after)
    rows = list((await session.scalars(
        statement.order_by(AutomationRun.id).limit(limit).with_for_update()
    )).all())
    complete = len(rows) < limit
    changed = 0
    from modules.automations.models import AutomationRunAction

    provisional: list[UUID] = []
    unavailable: list[UUID] = []
    for run in rows:
        if run.document_evidence_revoked:
            continue
        matched = (
            run.document_id == document_id and run.document_version_id is not None
            and run.payload.get("source_id") == str(source_id)
        )
        proven_version = run.document_version_id if matched else None
        event_id: UUID | None = None
        if matched:
            try:
                event_id = UUID(run.trigger_event_id or "")
                proof = await _legacy_event_matches_cleanup(
                    session, event_id, document_id=document_id, source_id=source_id, version_ids=version_ids,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                ) if str(event_id) == run.trigger_event_id and run.trigger_key == f"event:{event_id}" else None
            except (ValueError, TypeError):
                proof = None
            matched = proof is not None and proof.document_version_id == run.document_version_id
        if not matched and run.document_id is None and run.document_version_id is None:
            event_id = None
            if run.trigger_event_id is not None:
                try:
                    candidate = UUID(run.trigger_event_id)
                    if str(candidate) == run.trigger_event_id:
                        event_id = candidate
                except (ValueError, TypeError):
                    pass
            # The run formatter stores the original event ID twice; require the exact pair.
            if event_id is None and run.trigger_key.startswith("event:"):
                token = run.trigger_key.removeprefix("event:")
                try:
                    candidate = UUID(token)
                    if str(candidate) == token and run.trigger_key == f"event:{candidate}":
                        event_id = candidate
                except (ValueError, TypeError):
                    pass
            if event_id is not None and run.trigger_key == f"event:{event_id}" and run.trigger_event_id == str(event_id):
                proof = await _legacy_event_matches_cleanup(
                    session, event_id, document_id=document_id, source_id=source_id, version_ids=version_ids,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                matched = proof is not None
                proven_version = proof.document_version_id if proof is not None else None
        if run.document_id is not None and run.document_id != document_id:
            continue
        if (not matched and run.document_id is None and run.document_version_id is None
                and event_id is not None
                and await _event_belongs_elsewhere(session, str(event_id), document_id=document_id,
                        scope=scope, multi_workspace_enabled=multi_workspace_enabled)):
            continue
        contradictory = (
            run.document_id == document_id and run.document_version_id is not None
            and run.document_version_id not in version_ids
        )
        if contradictory:
            (unavailable if final_reference_page else provisional).append(run.id)
            continue
        if not matched:
            (unavailable if final_reference_page else provisional).append(run.id)
            continue
        actions = list((await session.scalars(
            select(AutomationRunAction).where(AutomationRunAction.run_id == run.id)
            .order_by(AutomationRunAction.ordinal).with_for_update()
        )).all())
        run.document_id = document_id
        run.document_version_id = proven_version
        run.document_evidence_revoked = True
        run.payload = _scrub_document_payload(run.payload)
        uncertain = any(action.status in {"in_flight", "requires_review"} for action in actions)
        for action in actions:
            if action.status in {"pending", "approved", "awaiting_approval"}:
                action.status, action.error_code, action.approved_session_hash = (
                    "skipped", "document_evidence_revoked", None,
                )
            elif action.status == "in_flight":
                action.status, action.error_code = "requires_review", "document_evidence_revoked"
        if uncertain:
            run.status, run.reason, run.finished_at = (
                "requires_review", "document_evidence_revoked", datetime.now(UTC),
            )
        elif run.status in {"queued", "running", "awaiting_approval"}:
            run.status, run.reason, run.finished_at = (
                "dropped", "document_evidence_revoked", datetime.now(UTC),
            )
        changed += 1
    if changed:
        await session.flush()
    return AutomationCleanupProgress(
        rows[-1].id if rows else after, complete, changed, operation_id, tuple(provisional), tuple(unavailable),
    )


async def fan_out_triggers(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, multi_workspace_enabled: bool,
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
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        pending = (
            AutomationTrigger.workspace_id == scope.workspace_id, AutomationTrigger.owner_id == _actor(scope),
            AutomationTrigger.status == "pending",
        )
        # Discover the bounded page without owner locks, then acquire its Source fences in UUID order.
        detached = (await session.scalars(
            select(AutomationTrigger).where(*pending)
            .order_by(AutomationTrigger.created_at).limit(100)
        )).all()
        source_ids: set[UUID] = set()
        for candidate in detached:
            if candidate.trigger_type != "new_document" or candidate.document_evidence_revoked:
                continue
            try:
                event_id = UUID(candidate.event_key)
            except (TypeError, ValueError):
                continue
            if str(event_id) != candidate.event_key:
                continue
            from modules.ingestion import public as ingestion
            proof = await ingestion.resolve_ready_event_provenance(
                session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            if proof is not None:
                source_ids.add(proof.source_id)
        from modules.sources import public as sources
        for source_id in sorted(source_ids, key=str):
            await sources.lock_retained_evidence_source(
                session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=fence)
        events = (await session.scalars(
            select(AutomationTrigger).where(*pending)
            .order_by(AutomationTrigger.created_at).limit(100).with_for_update(skip_locked=True)
            .execution_options(populate_existing=True))).all()
        # A producer may have inserted a new page member between discovery and row locking. Do not
        # discover and acquire its Source fence while holding inbox locks; defer the whole batch.
        for event in events:
            if event.trigger_type != "new_document" or event.document_evidence_revoked:
                continue
            try:
                event_id = UUID(event.event_key)
            except (TypeError, ValueError):
                continue
            if str(event_id) != event.event_key:
                continue
            from modules.ingestion import public as ingestion
            proof = await ingestion.resolve_ready_event_provenance(
                session, event_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            if proof is not None and proof.source_id not in source_ids:
                await session.rollback()
                return 0
        for event in events:
            if event.trigger_type == "new_document" and not await _trigger_evidence_current(
                session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
            ):
                event.status = "processed"
                event.document_evidence_revoked = True
                event.payload = _scrub_document_payload(event.payload)
                continue
            for rev in await live_rules(session, event.trigger_type, scope=scope):
                if event.trigger_type == "webhook" and rev.trigger.get("hook") != event.payload.get("hook"):
                    continue
                run_id = await plan_run(
                    session, rev=rev, trigger_type=event.trigger_type,
                    trigger_key=f"event:{event.event_key}", trigger_event_id=event.event_key, slot=None,
                    payload=event.payload, depth=event.depth + 1,
                    document_id=event.document_id, document_version_id=event.document_version_id,
                    origin_automation_id=event.origin_automation_id, origin_run_id=event.origin_run_id,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                created += run_id is not None
            event.status = "processed"
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return created


async def start_manual(
    session: AsyncSession, automation_id: UUID, expected_revision: int, client_request_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> RunRead:
    """Queue one run of a stored enabled rule at the expected revision (idempotent on the client id).

    Conditions are not applied (there is no event); admission limits and approvals still are.

    Raises:
        RunMissing: Rule absent, deleted or foreign.
        RunConflict: Stale revision or the rule is paused.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    head = await session.scalar(select(Automation).where(
        Automation.workspace_id == scope.workspace_id, Automation.owner_id == _actor(scope),
        Automation.id == automation_id, Automation.deleted_at.is_(None)
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
    assert rev is not None  # the head's current revision snapshot always exists
    await plan_run(
        session, rev=rev, trigger_type="manual", trigger_key=key, trigger_event_id=None,
        slot=None, payload={}, depth=1, origin_automation_id=None, origin_run_id=None, apply_conditions=False,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(
        session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    run = await session.scalar(select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.automation_id == head.id, AutomationRun.revision == head.revision,
        AutomationRun.trigger_key == key))
    assert run is not None  # plan_run committed the manual run above
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


async def list_runs(
    session: AsyncSession, automation_id: UUID, limit: int = 50, *, scope: Scope, multi_workspace_enabled: bool,
) -> RunPage:
    """Newest-first run history for a rule, retained after pause or delete (bounded to 100)."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    exists = await session.scalar(select(Automation.id).where(
        Automation.workspace_id == scope.workspace_id, Automation.owner_id == _actor(scope),
        Automation.id == automation_id))
    if exists is None:
        raise RunMissing
    runs = (await session.scalars(select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.automation_id == automation_id
    ).order_by(AutomationRun.created_at.desc()).limit(min(max(limit, 1), 100)))).all()
    return RunPage(items=await _reads(session, list(runs)))


def _approval_hash(run: AutomationRun, ordinal: int, spec: Mapping[str, Any], destination: str | None) -> str:
    """Bind an approval to the run, slot, immutable revision, exact action and webhook destination digest."""
    blob = json.dumps(
        [str(run.id), ordinal, run.revision, dict(spec), destination], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


async def _fence_current(session: AsyncSession, run: AutomationRun, *, scope: Scope, share: bool = False) -> bool:
    """True while the rule is live, enabled and still at the run's revision.

    ``share=True`` takes ``FOR SHARE`` on the head so an edit (which locks ``FOR UPDATE``) waits for
    the current action transaction; in-database effects therefore commit before any newer revision.
    """
    stmt = select(Automation).where(
        Automation.workspace_id == scope.workspace_id, Automation.owner_id == _actor(scope),
        Automation.id == run.automation_id)
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
    session: AsyncSession, session_hash: str, run_id: UUID, ordinal: int, approve: bool,
    settings: Settings, *, scope: Scope, multi_workspace_enabled: bool,
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
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    run = await session.scalar(select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.id == run_id).with_for_update())
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
    elif not await _run_evidence_current(
        session, run, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence,
    ):
        problem = ("document_evidence_unavailable", "Document trigger evidence is no longer available")
    elif not await _fence_current(session, run, scope=scope):
        problem = ("stale_revision", "Rule changed after this run was queued")
    elif _destination_stale(settings, spec, row.destination_revision):
        problem = ("stale_destination", "Webhook destination changed after this approval was requested")
    elif rev is not None and dependencies_missing(rev):
        problem = ("dependency_unavailable", "A required module is unavailable")
    if problem is not None:
        dropped = problem[0] in (
            "stale_revision", "stale_destination", "dependency_unavailable", "document_evidence_unavailable",
        )
        row.status, row.error_code = ("skipped" if dropped else "failed"), problem[0]
        _finish(run, "dropped" if dropped else "failed", problem[0])
        await _skip_pending(session, run.id, problem[0])
        await commit_with_replay(
            session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
        raise RunConflict(problem[0], problem[1])
    if approve:
        row.status, row.approved_session_hash = "approved", session_hash
        run.status, run.next_attempt_at, run.dispatched_at = "queued", now, None
    else:
        row.status, row.error_code = "denied", "denied_by_owner"
        _finish(run, "failed", "denied")
        await _skip_pending(session, run.id, "denied")
    await commit_with_replay(
        session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return (await _reads(session, [run]))[0]


async def _commit(session: AsyncSession, fence: AccessFence, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Fenced commit for a transaction that took the access fence first."""
    await commit_with_replay(
        session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)


async def _locked_run(session: AsyncSession, run_id: UUID, scope: Scope) -> AutomationRun | None:
    """FOR UPDATE select of one run inside the scope's workspace and owner."""
    return await session.scalar(select(AutomationRun).where(
        AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
        AutomationRun.id == run_id).with_for_update())


async def _mark(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, ordinal: int, status: str,
    code: str | None = None, reference: str | None = None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Persist an action outcome after locking its run then action, preserving revocation review state."""
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await _locked_run(session, run_id, scope)
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        if row is not None:
            if run is not None and (run.document_evidence_revoked or run.status == "requires_review"):
                if row.status != "succeeded":
                    row.status = "requires_review"
                    row.error_code = "document_evidence_revoked" if run.document_evidence_revoked else (
                        row.error_code or "run_requires_review"
                    )
                if reference is not None:
                    row.result_reference = reference
                row.approved_session_hash = None
            else:
                row.status, row.error_code, row.result_reference = status, code, reference
                row.approved_session_hash = None
            await commit_with_replay(
                session, [], scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)


async def _action_step(
    ctx: dict[str, Any], run_id: UUID, ordinal: int, *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Advance one action, durably pausing when its persisted owner module is disabled.

    Resumable: every row state is handled, so calling this after a crash converges. The fence is
    checked before every start, including exact Document trigger evidence. A revoked in-flight action
    remains review-only because the external effect may already have happened; it is never replayed.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await _locked_run(session, run_id, scope)
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        assert run is not None
        rev = await session.scalar(select(AutomationRevision).where(
            AutomationRevision.automation_id == run.automation_id, AutomationRevision.revision == run.revision))
        assert rev is not None
        spec = rev.actions[ordinal - 1]
        assert row is not None
        state = row.status
        if state == "succeeded":
            return "succeeded"
        if state in ("failed", "denied", "skipped"):
            return "failed"
        if not await _run_evidence_current(session, run, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence):
            if state in {"in_flight", "requires_review"}:
                # The dispatch boundary may already have been crossed; retain uncertainty and never replay it.
                assert row is not None
                row.status, row.error_code = "requires_review", "document_evidence_unavailable"
                _finish(run, "requires_review", "document_evidence_unavailable")
                assert run is not None
                await _skip_pending(session, run.id, "document_evidence_unavailable")
                await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                return "requires_review"
            assert row is not None
            row.status, row.error_code = "skipped", "document_evidence_unavailable"
            _finish(run, "dropped", "document_evidence_unavailable")
            assert run is not None
            await _skip_pending(session, run.id, "document_evidence_unavailable")
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "dropped"
        # run_agent is idempotent (client_request_id), so an interrupted one is simply attempted again.
        if state == "requires_review" or (state == "in_flight" and spec["type"] != "run_agent"):
            assert row is not None
            row.status, row.error_code = "requires_review", row.error_code or "ambiguous_after_restart"
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "requires_review"
        if state == "awaiting_approval":
            return "awaiting_approval"
        settings = cast(Settings, ctx["settings"])
        kind = spec["type"]
        stale = None
        if not await _fence_current(session, run, scope=scope, share=True):
            stale = "stale_revision"
        elif not await _action_modules_enabled(session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled):
            # Preserve the approved durable action for dispatch after its owner is re-enabled.
            return "paused"
        elif state == "approved" and _destination_stale(settings, spec, row.destination_revision):
            stale = "stale_destination"
        if stale is not None:
            assert row is not None
            row.status, row.error_code = "skipped", stale
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "dropped"
        if kind in APPROVAL_ACTIONS and state == "pending":
            try:
                destination = webhook_profile_revision(settings, spec["alias"]) if kind == "call_webhook" else None
            except ValueError:
                destination = None
            if kind == "call_webhook" and destination is None:
                assert row is not None
                row.status, row.error_code = "failed", "webhook_unavailable"
                await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                return "failed"
            assert row is not None
            row.destination_revision = destination
            row.status, row.approval_hash = "awaiting_approval", _approval_hash(run, ordinal, spec, destination)
            row.approval_expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours)
            assert rev is not None
            assert run is not None
            await emit(session, NotificationEmit(
                dedupe_key=f"automation-approval:{run.id}:{ordinal}", kind="automation.approval",
                title=rev.name[:300], body="An automation action is waiting for your approval."),
                scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "awaiting_approval"
        assert row is not None
        row.attempts += 1
        if kind in ("create_notification", "create_task"):
            return await _in_database_action(
                session, run, row, rev, spec, factory, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
        if kind == "generate_brief":
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)  # release locks; brief generation commits internally
            return await _generate_brief(ctx, run, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        session_hash, destination = row.approved_session_hash, row.destination_revision
        if kind == "call_webhook":
            assert row is not None
            row.approved_session_hash = None  # restore only if the final pre-send lifecycle fence pauses
        # run_agent keeps the digest until success: its idempotent start may be re-attempted.
        row.status = "in_flight"  # point of no return: committed before the external call
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if kind == "run_agent":
        assert rev is not None
        return await _start_agent(ctx, run, ordinal, spec, session_hash, rev.name, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await _send_webhook(ctx, run, ordinal, spec, factory, destination, session_hash, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _restore_paused_action(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, ordinal: int, *,
    status: str, session_hash: str | None, decrement_attempt: bool = True,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Return an unsent effect to its durable resumable state when lifecycle blocks admission."""
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await _locked_run(session, run_id, scope)
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal,
        ).with_for_update())
        if run is not None and (run.document_evidence_revoked or run.status == "requires_review"):
            if row is not None and row.status == "in_flight":
                row.status = "requires_review"
                row.error_code = "document_evidence_revoked" if run.document_evidence_revoked else (
                    row.error_code or "run_requires_review"
                )
                await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return
        if row is not None and row.status in {"pending", "in_flight"}:
            row.status, row.error_code = status, None
            row.approved_session_hash = session_hash
            if decrement_attempt:
                row.attempts = max(row.attempts - 1, 0)
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _in_database_action(
    session: AsyncSession, run: AutomationRun, row: AutomationRunAction, rev: AutomationRevision,
    spec: Mapping[str, Any], factory: async_sessionmaker[AsyncSession],
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> str:
    """Create a notification or task and flip the ledger row in one transaction (exactly-once)."""
    try:
        if spec["type"] == "create_notification":
            # The dedupe key makes even a repeated insert harmless.
            await emit(session, NotificationEmit(
                dedupe_key=f"automation:{run.id}:{row.ordinal}", kind="automation.rule", title=rev.name[:300],
                body=spec["message"], link=spec.get("link")),
                scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            reference = f"notification:{run.id}:{row.ordinal}"
        else:
            days = spec.get("due_in_days")
            task = await create_task_in_uow(session, TaskCreate(
                title=spec["title"], description=spec.get("description"),
                due_date=(datetime.now(UTC) + timedelta(days=days)).date() if days is not None else None),
                scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            reference = f"task:{task.id}"
        row.status, row.error_code, row.result_reference = "succeeded", None, reference
        await _commit(session, access_fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "succeeded"
    except (TaskConflict, ValidationError, ValueError):
        await session.rollback()
        await _mark(factory, run.id, row.ordinal, "failed", "rejected_by_owner_module", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "failed"
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        await session.rollback()
        return await _transient(factory, run.id, row.ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _transient(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, ordinal: int,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Retry a retry-safe action with bounded attempts; exhausted attempts fail the action.

    ``attempts`` was already incremented when the step started, so it counts the tries made.
    """
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        # Children are reached through the run, which must be inside the scope.
        if await _locked_run(session, run_id, scope) is None:
            return "failed"
        row = await session.scalar(select(AutomationRunAction).where(
            AutomationRunAction.run_id == run_id, AutomationRunAction.ordinal == ordinal).with_for_update())
        assert row is not None
        if row.attempts >= MAX_ACTION_ATTEMPTS:
            assert row is not None
            row.status, row.error_code = "failed", "retries_exhausted"
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "failed"
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "retry"


async def _generate_brief(
    ctx: dict[str, Any], run: AutomationRun, row: AutomationRunAction,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Generate through Dashboard only while the persisted trigger evidence remains eligible."""
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    try:
        async with factory() as session:
            # Model I/O follows, so admission is non-locking; the transaction is released after generation.
            fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            if not await _action_modules_enabled(session, row.action_type, scope=scope, multi_workspace_enabled=multi_workspace_enabled):
                await session.rollback()
                await _restore_paused_action(
                    factory, run.id, row.ordinal, status="pending", session_hash=None, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                return "paused"
            current_run = await session.scalar(select(AutomationRun).where(
                AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
                AutomationRun.id == run.id))
            if current_run is None or not await _run_evidence_current(session, current_run, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence):
                await session.rollback()
                await _mark(factory, run.id, row.ordinal, "skipped", "document_evidence_unavailable", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                return "dropped"
            schedule = await dashboard.read_schedule(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            day = datetime.now(UTC).astimezone(ZoneInfo(schedule.timezone)).date()
            brief = await dashboard.generate_brief(
                session, day, schedule.timezone, scope=scope, multi_workspace_enabled=multi_workspace_enabled, settings=cast(Settings, ctx["settings"]),
                redis=cast(Redis, ctx["redis"]), force=False)
        await _mark(
            factory, run.id, row.ordinal, "succeeded", None, f"brief:{getattr(brief, 'id', 'daily')}", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "succeeded"
    except dashboard.BriefEmpty:
        await _mark(factory, run.id, row.ordinal, "succeeded", None, "brief:empty", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "succeeded"
    except dashboard.BriefUnavailable:
        return await _transient(factory, run.id, row.ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return await _transient(factory, run.id, row.ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _automation_conversation(session: AsyncSession, automation_id: UUID, name: str, *, scope: Scope) -> UUID:
    """Return the rule's dedicated Chat conversation, creating it once (same model the Chat route creates).

    P07 profile runs require a live Chat link; this per-rule thread is where the owner opens the
    run, its activity and any approval the agent itself raises.
    """
    existing = await session.scalar(select(Conversation.id).where(
        Conversation.workspace_id == scope.workspace_id,
        Conversation.context_kind == "automation", Conversation.context_resource_id == automation_id,
        Conversation.archived.is_(False)).limit(1))
    if existing is not None:
        return existing
    conversation = Conversation(
        workspace_id=scope.workspace_id, actor_user_id=_actor(scope),
        title=f"Automation: {name}"[:255], context_kind="automation", context_resource_id=automation_id,
        metadata_json={})
    session.add(conversation)
    await session.flush()
    return conversation.id


async def _start_agent(
    ctx: dict[str, Any], run: AutomationRun, ordinal: int, spec: Mapping[str, Any], session_hash: str | None,
    rule_name: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Start the approved P07 profile run through ``create_profile_run_in_uow`` (row is already in_flight).

    Persisted Document trigger evidence is rechecked before Agent creation. The run uses the rule's
    ``profile_id`` at its current revision in the rule's Automation Chat
    conversation, so P07 approvals inside it work. ``client_request_id`` derives from
    (run, ordinal) and the owner-session scope, so a retry after a crash returns the same agent run
    rather than a second one; that is why this action is safe to resume. A rejection raised before
    the run row is written is a clean failure.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    registry = ctx.get("agent_tool_registry")
    if session_hash is None or registry is None:
        await _mark(factory, run.id, ordinal, "failed", "agent_unavailable", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "failed"
    try:
        async with factory() as session:
            # Config resolution may reach Redis/settings I/O, so admission is non-locking here.
            snapshot = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            config = await settings_public.get_ai_execution_config(
                session, cast(Settings, ctx["settings"]), cast(Redis, ctx["redis"]), scope=scope)
            # Lock order: admission fence first (same as other writers), then run, then Source locks.
            fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
            if fence != snapshot:
                await session.rollback()
                return await _transient(factory, run.id, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            current_run = await _locked_run(session, run.id, scope)
            if current_run is None or not await _run_evidence_current(session, current_run, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence):
                await session.rollback()
                await _mark(factory, run.id, ordinal, "skipped", "document_evidence_unavailable", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                return "dropped"
            if not await _action_modules_enabled(session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled):
                await session.rollback()
                await _restore_paused_action(
                    factory, run.id, ordinal, status="approved", session_hash=session_hash, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )
                return "paused"
            profile_id = spec["profile_id"]
            revision = await agents.current_profile_revision(
                session, profile_id, registry, config, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            conversation_id = await _automation_conversation(session, run.automation_id, rule_name, scope=scope)
            # The producer's title/condition metadata is never passed into the remote agent prompt.
            started = await agents.create_profile_run_in_uow(
                session, session_hash, profile_id,
                ProfileRunStart(
                    prompt=spec["instruction"], expected_profile_revision=revision,
                    conversation_id=conversation_id, client_request_id=str(uuid5(run.id, f"action:{ordinal}"))),
                registry, config, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        try:  # PostgreSQL is the queue; a lost push is replayed by the agent reconciler.
            await cast(ArqRedis, ctx["redis"]).enqueue_job(
                "process_agent_run", str(started.id), 1, _job_id=f"agent-run:{started.id}:1")
        except Exception:  # noqa: BLE001, S110  # best-effort cleanup/optional step; failure intentionally ignored
            pass
    except asyncio.CancelledError:
        raise  # stays in_flight; the idempotent start is attempted again on resume
    except HTTPException as exc:
        if exc.status_code >= 500:  # replay-storage blip etc.: retry, never a terminal rejection
            return await _transient(factory, run.id, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        await _mark(factory, run.id, ordinal, "failed", "agent_rejected", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "failed"
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return await _transient(factory, run.id, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    await _mark(factory, run.id, ordinal, "succeeded", None, f"agent_run:{started.id}", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return "succeeded"


async def _send_webhook(
    ctx: dict[str, Any], run: AutomationRun, ordinal: int, spec: Mapping[str, Any],
    factory: async_sessionmaker[AsyncSession], destination: str | None, session_hash: str | None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Send the approved webhook once through the shared SSRF-safe transport (row is already in_flight).

    The body is metadata only (rule, run, revision, depth), with no Document condition payload or
    title. Origin and depth go out as headers so a receiver that calls back into an inbound hook
    continues the same causal chain. Persisted evidence, revision and destination are checked after
    DNS and just before the socket write; a non-2xx or timeout after write begins is ``requires_review``.
    """
    settings = cast(Settings, ctx["settings"])
    lifecycle_blocked = False
    evidence_blocked = False
    publication_session: AsyncSession | None = None

    async def still_current() -> bool:
        """Hold evidence and rule fences until the transport's immediate socket-write boundary completes."""
        nonlocal lifecycle_blocked, evidence_blocked, publication_session
        if publication_session is not None:
            await publication_session.rollback()
            await publication_session.close()
            publication_session = None
        session = factory()
        try:
            # Held open through the socket write (network I/O), so admission is non-locking.
            fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            if not await _action_modules_enabled(session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled):
                lifecycle_blocked = True
                await session.rollback()
                await session.close()
                return False
            persisted = await session.scalar(select(AutomationRun).where(
                AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
                AutomationRun.id == run.id))
            if persisted is None or not await _run_evidence_current(session, persisted, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence):
                evidence_blocked = True
                await session.rollback()
                await session.close()
                return False
            valid = (
                await _fence_current(session, persisted, scope=scope, share=True)
                and not _destination_stale(settings, spec, destination)
            )
            if not valid:
                await session.rollback()
                await session.close()
                return False
            publication_session = session
            return True
        except Exception:
            await session.rollback()
            await session.close()
            raise

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
        await asyncio.shield(_mark(
            factory, run.id, ordinal, "requires_review", "cancelled_in_flight", scope=scope, multi_workspace_enabled=multi_workspace_enabled))
        raise
    finally:
        if publication_session is not None:
            await publication_session.rollback()
            await publication_session.close()
    if outcome == "succeeded":
        await _mark(factory, run.id, ordinal, "succeeded", None, f"webhook:{run.id}:{ordinal}", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "succeeded"
    if outcome == "unsent":
        if evidence_blocked:
            await _mark(factory, run.id, ordinal, "skipped", "document_evidence_unavailable", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "dropped"
        async with factory() as session:
            await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            current = await _fence_current(session, run, scope=scope)
            modules_enabled = await _action_modules_enabled(session, spec["type"], scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if lifecycle_blocked or not modules_enabled:
            await _restore_paused_action(
                factory, run.id, ordinal, status="approved", session_hash=session_hash, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            return "paused"
        if not current or _destination_stale(settings, spec, destination):
            await _mark(
                factory, run.id, ordinal, "skipped", "stale_revision" if not current else "stale_destination", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "dropped"
        await _mark(factory, run.id, ordinal, "failed", "webhook_unsent", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return "failed"
    await _mark(
        factory, run.id, ordinal, "requires_review", "outcome_unknown", f"webhook:{run.id}:{ordinal}", scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return "requires_review"


async def _settle(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, outcome: str,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> str:
    """Move the run to the status implied by the last action outcome and persist it."""
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await _locked_run(session, run_id, scope)
        if run is None:
            return "dropped"
        if run.document_evidence_revoked or run.status == "requires_review":
            if run.document_evidence_revoked:
                _finish(run, "requires_review", "document_evidence_revoked")
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=15 * 2 ** min(run.attempts, 5))
        else:
            status = {"succeeded": "succeeded", "failed": "failed", "dropped": "dropped",
                      "requires_review": "requires_review"}[outcome]
            _finish(run, status, None if outcome == "succeeded" else outcome)
            if outcome != "succeeded":
                await _skip_pending(session, run_id, "prior_action_" + outcome)
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        return run.status


async def _heartbeat(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Refresh ``updated_at`` so a long multi-action run is not mistaken for a dead worker."""
    async with factory() as session:
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await _locked_run(session, run_id, scope)
        assert run is not None
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
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    multi_workspace_enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    rid, now = UUID(run_id), datetime.now(UTC)
    async with factory() as session:
        identity = (await session.execute(
            select(AutomationRun.workspace_id, AutomationRun.owner_id).where(AutomationRun.id == rid))).first()
        if identity is None:
            return "noop"
        owner = await workspaces.resolve_workspace_owner_context(
            session, identity[0], multi_workspace_enabled=multi_workspace_enabled)
        if owner is None or owner.user_id != identity[1]:
            return "noop"
        scope = InternalJobScope(
            workspace_id=identity[0], actor_user_id=owner.user_id, membership_revision=owner.membership_revision)
        if not await settings_public.module_is_enabled(
            session, "automations", scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            return "paused"
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        run = await session.scalar(select(AutomationRun).where(
            AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
            AutomationRun.id == rid).with_for_update(skip_locked=True))
        if run is None or run.status != "queued" or run.next_attempt_at > now:
            return "noop"
        busy = await session.scalar(select(func.count()).select_from(AutomationRun).where(
            AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope),
            AutomationRun.automation_id == run.automation_id, AutomationRun.id != rid,
            AutomationRun.status == "running", AutomationRun.updated_at > now - RUNNING_STALE_AFTER))
        if busy:
            run.next_attempt_at, run.dispatched_at = now + timedelta(seconds=5), None
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "busy"
        if run.attempts >= MAX_RUN_ATTEMPTS:
            _finish(run, "failed", "attempts_exhausted")
            await _skip_pending(session, rid, "attempts_exhausted")
            await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            return "failed"
        run.status, run.attempts = "running", run.attempts + 1
        total = await session.scalar(select(func.count()).select_from(AutomationRunAction).where(
            AutomationRunAction.run_id == rid))
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    for ordinal in range(1, (total or 0) + 1):
        await _heartbeat(factory, rid, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        outcome = await _action_step(ctx, rid, ordinal, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if outcome != "succeeded":
            return await _settle(
                factory, rid, outcome, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await _settle(factory, rid, "succeeded", scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def dispatch_runs(
    factory: async_sessionmaker[AsyncSession], redis: ArqRedis, *, scope: Scope, multi_workspace_enabled: bool,
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
        fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
        mine = (AutomationRun.workspace_id == scope.workspace_id, AutomationRun.owner_id == _actor(scope))
        expired = (await session.scalars(select(AutomationRunAction).join(
            AutomationRun, AutomationRun.id == AutomationRunAction.run_id,
        ).where(
            *mine, AutomationRunAction.status == "awaiting_approval", AutomationRunAction.approval_expires_at < now
        ).limit(50).with_for_update(of=AutomationRunAction, skip_locked=True))).all()
        for row in expired:
            row.status, row.error_code = "failed", "approval_expired"
            run = await _locked_run(session, row.run_id, scope)
            assert run is not None
            _finish(run, "failed", "approval_expired")
            await _skip_pending(session, run.id, "approval_expired")
        runs = (await session.scalars(select(AutomationRun).where(
            *mine,
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
        await _commit(session, fence, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return enqueued
