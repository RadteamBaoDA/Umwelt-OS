"""Owner-facing automation rule service: CRUD with immutable revisions, validation and dry preview.

Later tasks (dispatch, scheduling, UI) consume this module only. Writes are owner-scoped and
revision fenced; every definition change appends an ``AutomationRevision`` that is never
rewritten, so a run can cite the exact definition that fired. Nothing here queues work,
calls a model, sends a webhook or creates a task: preview is pure evaluation.
"""

from __future__ import annotations

from core.telemetry import RunMeta as _RunMeta
from modules.automations.models import AutomationRun as _AutomationRun

import hashlib
import json
from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from modules.chat.public import Conversation
from modules.automations.conditions import TRIGGER_FIELDS, evaluate, validate_sample
from modules.automations.execution import (
    ACTION_MODULE,
    TRIGGER_MODULE,
    AutomationCleanupProgress,
    RunConflict,
    RunMissing,
    decide_action,
    enqueue_trigger,
    list_runs,
    loop_guard,
    origin_for_reference,
    scrub_document_runs,
    scrub_document_triggers,
    start_manual,
)
from modules.automations.models import Automation, AutomationRevision, AutomationRunAction
from modules.automations.schemas import (
    MAX_REVISION,
    AutomationCreate,
    AutomationDefinition,
    AutomationPage,
    AutomationRead,
    AutomationUpdate,
    CapabilitiesRead,
    CapabilityItem,
    PlannedAction,
    PreviewRequest,
    PreviewResult,
)
from modules.dashboard import public as dashboard
from modules.tools.public import webhook_aliases

MAX_AUTOMATIONS_PER_OWNER = 100


async def unresolved_backup_effects(session: AsyncSession) -> dict[str, int]:
    """Project automation action states with an unproven external effect outcome.

    Queued and approved actions have not crossed the external dispatch boundary and are safe
    to capture; only in-flight and review-required action journal rows block a snapshot.
    """
    rows = (await session.execute(
        select(AutomationRunAction.status, func.count()).where(
            AutomationRunAction.status.in_({"in_flight", "requires_review"}),
        ).group_by(AutomationRunAction.status)
    )).all()
    return {str(status): int(count) for status, count in rows}

# External or model-spending actions keep P07 approval semantics when executed later.
APPROVAL_ACTIONS = {"run_agent", "call_webhook"}


class AutomationInvalid(Exception):
    """Raised when a definition is well-formed but violates dependency or allowlist rules."""


class AutomationMissing(Exception):
    """Raised for an absent, soft-deleted or foreign-owned rule."""


class PauseBeforeBriefEdit(AutomationInvalid):
    """Raised when an enabled rule is edited into a daily brief schedule without an explicit enable."""


class AutomationConflict(Exception):
    """Raised on a stale revision, exhausted counter or per-owner quota."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Keep a stable machine code and optional current revision for HTTP 409."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


def validate_dependencies(definition: AutomationDefinition, registry: Mapping[str, Any]) -> None:
    """Require every module the trigger and actions rely on to be registered and enabled.

    Raises:
        AutomationInvalid: Naming the missing or disabled module ids.
    """
    needed = {TRIGGER_MODULE[definition.trigger.type]} | {ACTION_MODULE[a.type] for a in definition.actions}
    bad = sorted(m for m in needed if m is not None and (m not in registry or not registry[m].enabled))
    if bad:
        raise AutomationInvalid(f"required modules unavailable: {', '.join(bad)}")


def validate_webhook_targets(definition: AutomationDefinition, settings: Settings) -> None:
    """Allow call_webhook only for aliases in the deployment-owned WEBHOOK_PROFILES allowlist.

    The rule never carries a URL; an alias that is absent or disabled is rejected at save time
    so an owner cannot smuggle an arbitrary destination into a scheduled action.
    """
    aliases = {a.alias for a in definition.actions if a.type == "call_webhook"}
    if aliases:
        try:
            allowed = webhook_aliases(settings)
        except ValueError as exc:
            raise AutomationInvalid("webhook allowlist unavailable") from exc
        unknown = sorted(aliases - allowed)
        if unknown:
            raise AutomationInvalid(f"webhook alias not allowlisted: {', '.join(unknown)}")


def validate_definition(definition: AutomationDefinition, registry: Mapping[str, Any], settings: Settings) -> None:
    """Run all save-time checks beyond schema shape (dependencies, webhook allowlist, loop pairs)."""
    pair = loop_guard(definition.trigger.model_dump(mode="json"), [a.model_dump(mode="json") for a in definition.actions])
    if pair:
        raise AutomationInvalid(f"trigger and actions form a self-triggering loop ({pair})")
    validate_dependencies(definition, registry)
    validate_webhook_targets(definition, settings)


def _owns_brief_slot(enabled: bool, trigger: Mapping[str, Any], actions: list[Any]) -> bool:
    """True when an enabled rule would generate the daily brief on a schedule (needs the brief slot)."""
    return enabled and trigger["type"] == "schedule" and any(a["type"] == "generate_brief" for a in actions)


async def _sync_brief_slot(session: AsyncSession, owner_id: int, head: Automation, parts: tuple[Any, Any, Any],
                           *, explicit_enable: bool, was_owner_candidate: bool) -> None:
    """Keep the daily_brief slot single-owner, in the caller's transaction (committed by the caller).

    Invariant: the slot belongs to the internal brief cron unless one enabled schedule+generate_brief
    automation has been explicitly enabled. Transfer happens only on an explicit enable (never as a
    side effect of editing); disable, delete or no longer qualifying hands it back.
    """
    if _owns_brief_slot(head.enabled, parts[0], parts[2]):
        if explicit_enable:
            try:
                await dashboard.claim_brief_slot(session, owner_id, head.id)
            except dashboard.BriefSlotOwned as exc:
                raise AutomationConflict("brief_slot_owned", "Another automation owns the daily brief schedule") from exc
        elif was_owner_candidate is False:
            raise PauseBeforeBriefEdit("pause the rule, then enable it to take over the daily brief schedule")
    else:
        await dashboard.release_brief_slot(session, owner_id, head.id)


def _dump(definition: AutomationDefinition) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Serialize a validated definition to plain JSON parts for storage."""
    return (
        definition.trigger.model_dump(mode="json"),
        [c.model_dump(mode="json") for c in definition.conditions],
        [a.model_dump(mode="json") for a in definition.actions],
    )


def _hash(trigger: dict[str, Any], conditions: list[Any], actions: list[Any], enabled: bool) -> str:
    """Content-address a revision so identical snapshots are recognisable."""
    blob = json.dumps([trigger, conditions, actions, enabled], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _read(head: Automation, rev: AutomationRevision) -> AutomationRead:
    """Join a rule head with its current revision snapshot."""
    return AutomationRead(
        id=head.id, name=head.name, enabled=head.enabled, revision=head.revision,
        trigger=rev.trigger, conditions=rev.conditions, actions=rev.actions,
        created_at=head.created_at, updated_at=head.updated_at,
    )


async def _current_revision(session: AsyncSession, head: Automation) -> AutomationRevision:
    """Load the immutable snapshot matching the head's revision number."""
    return await session.scalar(select(AutomationRevision).where(
        AutomationRevision.automation_id == head.id, AutomationRevision.revision == head.revision,
    ))


def _append_revision(session: AsyncSession, head: Automation, trigger: dict[str, Any],
                     conditions: list[Any], actions: list[Any]) -> AutomationRevision:
    """Stage a new append-only snapshot for the head's current revision number."""
    rev = AutomationRevision(
        automation_id=head.id, revision=head.revision, name=head.name, enabled=head.enabled,
        trigger=trigger, conditions=conditions, actions=actions,
        content_hash=_hash(trigger, conditions, actions, head.enabled),
    )
    session.add(rev)
    return rev


async def _owned_head(session: AsyncSession, owner_id: int, automation_id: UUID, *, lock: bool = False) -> Automation:
    """Fetch a live owned rule head, optionally row-locked for revision-fenced writes."""
    stmt = select(Automation).where(
        Automation.id == automation_id, Automation.owner_id == owner_id, Automation.deleted_at.is_(None))
    head = await session.scalar(stmt.with_for_update() if lock else stmt)
    if head is None:
        raise AutomationMissing
    return head


async def create_automation(session: AsyncSession, owner_id: int, payload: AutomationCreate,
                            registry: Mapping[str, Any], settings: Settings, *,
                            automation_id: UUID | None = None, commit: bool = True) -> AutomationRead:
    """Validate and persist a new rule at revision 1, committing head and snapshot atomically.

    ``automation_id`` (stable seed ids) and ``commit=False`` (flush only, caller owns the
    transaction) exist for the explicit demo seed; routes and tools use the defaults.
    """
    validate_definition(payload, registry, settings)
    live = (await session.scalars(select(Automation.id).where(
        Automation.owner_id == owner_id, Automation.deleted_at.is_(None)).limit(MAX_AUTOMATIONS_PER_OWNER))).all()
    if len(live) >= MAX_AUTOMATIONS_PER_OWNER:
        raise AutomationConflict("quota_exceeded", "Automation limit reached")
    head = Automation(owner_id=owner_id, name=payload.name, enabled=payload.enabled, revision=1)
    if automation_id is not None:
        head.id = automation_id
    session.add(head)
    await session.flush()
    rev = _append_revision(session, head, *_dump(payload))
    if payload.enabled:
        await _sync_brief_slot(session, owner_id, head, (rev.trigger, rev.conditions, rev.actions),
                               explicit_enable=True, was_owner_candidate=False)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return _read(head, rev)


async def update_automation(session: AsyncSession, owner_id: int, automation_id: UUID, payload: AutomationUpdate,
                            registry: Mapping[str, Any], settings: Settings) -> AutomationRead:
    """Apply a revision-fenced patch by appending a new snapshot; old revisions stay untouched.

    Pausing (``enabled=False``) also bumps the revision so queued work fenced on the old
    revision is invalidated by dispatch while its history remains.
    """
    head = await _owned_head(session, owner_id, automation_id, lock=True)
    if head.revision != payload.expected_revision:
        raise AutomationConflict("stale_revision", f"Rule is at revision {head.revision}", head.revision)
    if head.revision >= MAX_REVISION:
        raise AutomationConflict("revision_exhausted", "Revision counter exhausted", head.revision)
    current = await _current_revision(session, head)
    if payload.trigger is not None or payload.conditions is not None or payload.actions is not None:
        try:
            merged = AutomationDefinition(
                trigger=payload.trigger if payload.trigger is not None else current.trigger,
                conditions=payload.conditions if payload.conditions is not None else current.conditions,
                actions=payload.actions if payload.actions is not None else current.actions,
            )
        except ValidationError as exc:
            raise AutomationInvalid("definition is invalid for the selected trigger") from exc
        validate_definition(merged, registry, settings)
        parts = _dump(merged)
    else:
        # Pause/resume/rename copies the stored snapshot verbatim: a rule that became invalid
        # later (changed fields or disabled module) must still be pausable.
        parts = (current.trigger, current.conditions, current.actions)
    was_enabled = head.enabled
    was_candidate = _owns_brief_slot(was_enabled, current.trigger, current.actions)
    if payload.name is not None:
        head.name = payload.name
    if payload.enabled is not None:
        head.enabled = payload.enabled
    head.revision += 1
    rev = _append_revision(session, head, *parts)
    await _sync_brief_slot(session, owner_id, head, parts, explicit_enable=head.enabled and not was_enabled,
                           was_owner_candidate=was_candidate)
    await session.commit()
    return _read(head, rev)


async def delete_automation(session: AsyncSession, owner_id: int, automation_id: UUID, expected_revision: int) -> None:
    """Soft-delete and disable a rule; revisions are retained for run history.

    Deleting also bumps the revision and appends a disabled snapshot, so dispatch has a single
    fence: any queued or approved work citing the old revision no longer matches and is dropped.
    """
    head = await _owned_head(session, owner_id, automation_id, lock=True)
    if head.revision != expected_revision:
        raise AutomationConflict("stale_revision", f"Rule is at revision {head.revision}", head.revision)
    if head.revision >= MAX_REVISION:
        raise AutomationConflict("revision_exhausted", "Revision counter exhausted", head.revision)
    current = await _current_revision(session, head)
    head.deleted_at = datetime.now(UTC)
    head.enabled = False
    head.revision += 1
    _append_revision(session, head, current.trigger, current.conditions, current.actions)
    await dashboard.release_brief_slot(session, owner_id, head.id)
    await session.commit()


async def get_automation(session: AsyncSession, owner_id: int, automation_id: UUID) -> AutomationRead:
    """Return one live owned rule at its current revision."""
    head = await _owned_head(session, owner_id, automation_id)
    return _read(head, await _current_revision(session, head))


def capabilities(registry: Mapping[str, Any], settings: Settings) -> CapabilitiesRead:
    """Describe trigger/action types the editor may offer, with availability from the live module registry."""
    def usable(module: str | None) -> bool:
        """A type is usable when its owning module is registered and enabled (schedule has none)."""
        return module is None or (module in registry and registry[module].enabled)

    try:
        aliases = sorted(webhook_aliases(settings))
    except ValueError:
        aliases = []
    triggers = [CapabilityItem(
        type=name, module=TRIGGER_MODULE[name], fields=dict(TRIGGER_FIELDS[name]),
        available=usable(TRIGGER_MODULE[name]),
        reason=None if usable(TRIGGER_MODULE[name]) else "module_disabled",
    ) for name in TRIGGER_FIELDS]
    actions = [CapabilityItem(
        type=name, module=module, available=usable(module), requires_approval=name in APPROVAL_ACTIONS,
        reason=None if usable(module) else "module_disabled",
    ) for name, module in ACTION_MODULE.items()]
    return CapabilitiesRead(triggers=triggers, actions=actions, webhook_aliases=aliases)


async def get_automation_conversation_id(session: AsyncSession, owner_id: int, automation_id: UUID) -> UUID | None:
    """Return the rule's Chat conversation id (hidden from the default Chat list) or None before any agent run."""
    await _owned_head(session, owner_id, automation_id)
    return await session.scalar(select(Conversation.id).where(
        Conversation.context_kind == "automation", Conversation.context_resource_id == automation_id,
        Conversation.archived.is_(False)).limit(1))


async def list_automations(session: AsyncSession, owner_id: int, *, enabled: bool | None = None,
                           trigger_type: str | None = None) -> AutomationPage:
    """List live owned rules (bounded), optionally by enabled flag or trigger type.

    Used by dispatch to find candidate rules for an incoming trigger.
    """
    stmt = select(Automation).where(Automation.owner_id == owner_id, Automation.deleted_at.is_(None))
    if enabled is not None:
        stmt = stmt.where(Automation.enabled == enabled)
    heads = (await session.scalars(stmt.order_by(Automation.created_at).limit(MAX_AUTOMATIONS_PER_OWNER))).all()
    items = []
    for head in heads:
        rev = await _current_revision(session, head)
        if trigger_type is None or rev.trigger["type"] == trigger_type:
            items.append(_read(head, rev))
    return AutomationPage(items=items)


async def get_revision(session: AsyncSession, owner_id: int, automation_id: UUID, revision: int) -> AutomationRead:
    """Return the exact immutable snapshot a run cites, even if the rule was paused or deleted since."""
    head = await session.scalar(select(Automation).where(
        Automation.id == automation_id, Automation.owner_id == owner_id))
    rev = await session.scalar(select(AutomationRevision).where(
        AutomationRevision.automation_id == automation_id, AutomationRevision.revision == revision))
    if head is None or rev is None:
        raise AutomationMissing
    return AutomationRead(
        id=head.id, name=rev.name, enabled=rev.enabled, revision=rev.revision, trigger=rev.trigger,
        conditions=rev.conditions, actions=rev.actions, created_at=rev.created_at, updated_at=rev.created_at)


def evaluate_conditions(conditions: list[dict[str, Any]], payload: Mapping[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Public pass-through to the deterministic evaluator for dispatch (T2) to reuse."""
    return evaluate(conditions, dict(payload))


async def preview(session: AsyncSession, owner_id: int, request: PreviewRequest) -> PreviewResult:
    """Dry-run conditions against a supplied metadata sample and list the actions that would run.

    Pure evaluation: no job is queued, no model is called, no webhook is sent and no row is
    written. A stored rule is read through the owner fence; the sample is checked against the
    trigger's declared fields. Reasons carry codes only, never sample or rule content.
    """
    if request.definition is not None:
        trigger, conditions, actions = _dump(request.definition)
    else:
        read = await get_automation(session, owner_id, request.automation_id)
        trigger, conditions, actions = read.trigger, read.conditions, read.actions
        try:
            validate_sample(trigger["type"], request.sample)
        except ValueError as exc:
            raise AutomationInvalid(str(exc)) from exc
    matched, reasons = evaluate(conditions, request.sample)
    planned = [
        PlannedAction(type=a["type"], module=ACTION_MODULE[a["type"]], requires_approval=a["type"] in APPROVAL_ACTIONS)
        for a in actions
    ] if matched else []
    return PreviewResult(matched=matched, reasons=reasons, planned_actions=planned)


__all__ = [
    "AutomationConflict", "AutomationInvalid", "PauseBeforeBriefEdit", "capabilities", "AutomationMissing", "RunConflict", "RunMissing",
    "AutomationCleanupProgress", "scrub_document_runs", "scrub_document_triggers",
    "create_automation", "decide_action", "delete_automation", "enqueue_trigger", "evaluate_conditions",
    "get_automation", "get_automation_conversation_id", "get_revision", "list_automations", "list_runs", "origin_for_reference", "preview",
    "start_manual",
    "update_automation", "validate_definition", "list_run_meta", "get_run_meta_by_id",
]


async def list_run_meta(session: AsyncSession, limit: int) -> list[_RunMeta]:
    """Return at most ``limit`` (<=100) newest automation runs as metadata only (no payload, reason text excluded)."""
    rows = await session.scalars(select(_AutomationRun).order_by(_AutomationRun.created_at.desc()).limit(min(limit, 100)))
    return [_RunMeta(kind="automation", id=str(r.id), status=r.status, created_at=r.created_at,
                     updated_at=r.updated_at, finished_at=r.finished_at,
                     origin_run_id=str(r.origin_run_id) if r.origin_run_id else None)
            for r in rows]


async def get_run_meta_by_id(session: AsyncSession, run_id: UUID) -> _RunMeta | None:
    """Return one metadata-only run projection by its indexed primary key."""
    row = await session.get(_AutomationRun, run_id)
    if row is None:
        return None
    return _RunMeta(kind="automation", id=str(row.id), status=row.status, created_at=row.created_at,
                    updated_at=row.updated_at, finished_at=row.finished_at,
                    origin_run_id=str(row.origin_run_id) if row.origin_run_id else None)
