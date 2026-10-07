"""Pydantic contracts for automation rules: discriminated triggers/actions, conditions, preview."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.automations.conditions import Operator, check_condition, validate_sample
from modules.automations.scheduler import validate_schedule
from modules.notifications.public import NotificationEmit

MAX_CONDITIONS = 20
MAX_ACTIONS = 10  # bounds fan-out per firing
MAX_REVISION = 9_007_199_254_740_991
_CRON_TOKEN = re.compile(r"^[0-9*/,\-]{1,40}$")
_STRICT = ConfigDict(extra="forbid")


class _Trigger(BaseModel):
    """Base for trigger variants; extra keys are rejected so unknown parameters never persist."""

    model_config = _STRICT


class ScheduleTrigger(_Trigger):
    """Fire on a 5-field cron expression in an IANA timezone."""

    type: Literal["schedule"]
    cron: str = Field(max_length=120)
    timezone: str = Field(default="UTC", max_length=64)

    @field_validator("cron")
    @classmethod
    def cron_shape(cls, value: str) -> str:
        """Accept five whitespace-separated tokens of digits and ``*/,-`` that the scheduler can parse.

        Range, step and "never fires" errors surface here (422) rather than at the first slot.
        """
        tokens = value.split()
        if len(tokens) != 5 or not all(_CRON_TOKEN.match(t) for t in tokens):
            raise ValueError("cron must have five fields of digits and */,-")
        cron = " ".join(tokens)
        validate_schedule(cron, "UTC", spends_model=False)
        return cron

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        """Reject unknown IANA zones."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class NewEventTrigger(_Trigger):
    """Fire when a timeline/domain event is recorded."""

    type: Literal["new_event"]


class NewDocumentTrigger(_Trigger):
    """Fire when a document version is ingested."""

    type: Literal["new_document"]


class EntityChangedTrigger(_Trigger):
    """Fire when a canonical entity is created or changed."""

    type: Literal["entity_changed"]


class TaskDueTrigger(_Trigger):
    """Fire ``lead_minutes`` before a task is due."""

    type: Literal["task_due"]
    lead_minutes: int = Field(default=0, ge=0, le=10_080)


class GoalDeadlineTrigger(_Trigger):
    """Fire ``lead_days`` before a goal deadline."""

    type: Literal["goal_deadline"]
    lead_days: int = Field(default=0, ge=0, le=365)


class WebhookTrigger(_Trigger):
    """Fire on an inbound webhook delivered under a named hook."""

    type: Literal["webhook"]
    hook: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")


class ConnectorSyncResultTrigger(_Trigger):
    """Fire when a connector sync run reports a result."""

    type: Literal["connector_sync_result"]


Trigger = Annotated[
    ScheduleTrigger | NewEventTrigger | NewDocumentTrigger | EntityChangedTrigger | TaskDueTrigger | GoalDeadlineTrigger | WebhookTrigger | ConnectorSyncResultTrigger,
    Field(discriminator="type"),
]


class Condition(BaseModel):
    """One whitelisted comparison; the value is typed later against the trigger's field."""

    model_config = _STRICT

    field: str = Field(min_length=1, max_length=64)
    operator: Operator
    value: str | int | float | bool | list[str | int | float | bool]


class _Action(BaseModel):
    """Base for action variants."""

    model_config = _STRICT


class RunAgentAction(_Action):
    """Start a P07 agent run for a fixed-roster profile (approval rules stay with the run)."""

    type: Literal["run_agent"]
    profile_id: Literal["knowledge", "research", "personal", "project", "news", "planning", "automation"]
    instruction: str = Field(min_length=1, max_length=2000)


class CreateTaskAction(_Action):
    """Create an inbox task through the tasks module."""

    type: Literal["create_task"]
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10_000)
    due_in_days: int | None = Field(default=None, ge=0, le=365)


class CreateNotificationAction(_Action):
    """Emit an owner notification; ``link`` must be a relative in-app path."""

    type: Literal["create_notification"]
    message: str = Field(min_length=1, max_length=300)
    link: str | None = Field(default=None, max_length=300)

    @field_validator("link")
    @classmethod
    def in_app_link(cls, value: str | None) -> str | None:
        """Reuse the notification module's relative-path rule instead of duplicating it."""
        if value is not None:
            NotificationEmit(dedupe_key="x", kind="automation", link=value)
        return value


class GenerateBriefAction(_Action):
    """Generate the daily brief through the dashboard module."""

    type: Literal["generate_brief"]
    scope: Literal["daily"] = "daily"


class CallWebhookAction(_Action):
    """Call a deployment-allowlisted webhook alias; URLs are never accepted from the rule."""

    type: Literal["call_webhook"]
    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    event: str = Field(min_length=1, max_length=100)


Action = Annotated[
    RunAgentAction | CreateTaskAction | CreateNotificationAction | GenerateBriefAction | CallWebhookAction,
    Field(discriminator="type"),
]


class AutomationDefinition(BaseModel):
    """The revisioned part of a rule: trigger, AND-ed conditions and ordered actions."""

    model_config = _STRICT

    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def conditions_match_trigger(self) -> AutomationDefinition:
        """Check every condition against the declared fields and, for schedules, the interval floor.

        A schedule faster than every five minutes is refused when any action is ``run_agent``
        (model spend); otherwise the floor is one minute.
        """
        for cond in self.conditions:
            check_condition(self.trigger.type, cond.field, cond.operator, cond.value)
        if self.trigger.type == "schedule":
            validate_schedule(
                self.trigger.cron, self.trigger.timezone,
                spends_model=any(a.type == "run_agent" for a in self.actions))
        return self


class AutomationCreate(AutomationDefinition):
    """Create payload: definition plus display name and initial enabled flag."""

    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class AutomationUpdate(BaseModel):
    """Revision-fenced patch; any change appends a new immutable revision."""

    model_config = _STRICT

    expected_revision: int = Field(ge=1, le=MAX_REVISION)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    trigger: Trigger | None = None
    conditions: list[Condition] | None = Field(default=None, max_length=MAX_CONDITIONS)
    actions: list[Action] | None = Field(default=None, min_length=1, max_length=MAX_ACTIONS)


class AutomationRead(BaseModel):
    """Public projection of a rule at its current revision."""

    id: UUID
    name: str
    enabled: bool
    revision: int
    trigger: dict[str, Any]
    conditions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class AutomationPage(BaseModel):
    """Bounded list of owner rules."""

    items: list[AutomationRead]


class PreviewRequest(BaseModel):
    """Dry preview of an inline definition or a stored rule against a supplied metadata sample."""

    model_config = _STRICT

    automation_id: UUID | None = None
    definition: AutomationDefinition | None = None
    sample: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def exactly_one_source(self) -> PreviewRequest:
        """Require either a stored rule or an inline definition, and a sample valid for its trigger."""
        if (self.automation_id is None) == (self.definition is None):
            raise ValueError("provide exactly one of automation_id or definition")
        if self.definition is not None:
            validate_sample(self.definition.trigger.type, self.sample)
        return self


class PlannedAction(BaseModel):
    """Type-level description of what would run; carries no rule content."""

    type: str
    module: str
    requires_approval: bool


class PreviewResult(BaseModel):
    """Outcome of a dry run: matched flag, per-condition reason codes and planned actions."""

    matched: bool
    reasons: list[dict[str, Any]]
    planned_actions: list[PlannedAction]



class ManualRunRequest(BaseModel):
    """Start one run of a stored rule now; the client id makes retries return the same run."""

    model_config = _STRICT

    client_request_id: UUID
    expected_revision: int = Field(ge=1, le=MAX_REVISION)


class DecisionRequest(BaseModel):
    """Owner decision on one action that is waiting for approval."""

    model_config = _STRICT

    decision: Literal["approve", "deny"]


class RunActionRead(BaseModel):
    """Persisted outcome of one ordered action; carries codes and references, never content."""

    ordinal: int
    type: str
    status: str
    attempts: int
    error_code: str | None
    result_reference: str | None
    approval_expires_at: datetime | None


class RunRead(BaseModel):
    """Run record citing the immutable rule revision, its causal depth and action outcomes."""

    id: UUID
    automation_id: UUID
    revision: int
    trigger_type: str
    trigger_event_id: str | None
    scheduled_slot: datetime | None
    depth: int
    origin_automation_id: UUID | None
    origin_run_id: UUID | None
    status: str
    reason: str | None
    attempts: int
    created_at: datetime
    finished_at: datetime | None
    actions: list[RunActionRead]


class RunPage(BaseModel):
    """Newest-first bounded run history for one rule."""

    items: list[RunRead]


class CapabilityItem(BaseModel):
    """One trigger or action type with its owning module, availability and declared fields."""

    type: str
    module: str | None
    available: bool
    reason: str | None = None
    requires_approval: bool = False
    fields: dict[str, str] = Field(default_factory=dict)


class CapabilitiesRead(BaseModel):
    """Schema-backed options for the rule editor; webhook entries are alias names, never URLs."""

    triggers: list[CapabilityItem]
    actions: list[CapabilityItem]
    webhook_aliases: list[str]
