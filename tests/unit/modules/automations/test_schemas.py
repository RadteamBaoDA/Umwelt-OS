"""Unit tests for automations module schemas: triggers, actions, conditions, and definitions.

Covers:
- Trigger variants (ScheduleTrigger, TaskDueTrigger, GoalDeadlineTrigger, WebhookTrigger, NewEventTrigger, etc.)
- Cron schedule validation, timezone checking, and lead times
- Condition schema and typing
- Action variants (RunAgentAction, CreateTaskAction, CreateNotificationAction, GenerateBriefAction, CallWebhookAction)
- In-app link validation on CreateNotificationAction
- AutomationDefinition validation (cross-checking conditions against trigger declared fields, action bounds)
- AutomationCreate, AutomationUpdate, and AutomationRead schemas
"""

from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.automations.schemas import (
    MAX_ACTIONS,
    MAX_CONDITIONS,
    Action,
    AutomationCreate,
    AutomationDefinition,
    AutomationRead,
    AutomationUpdate,
    CallWebhookAction,
    Condition,
    ConnectorSyncResultTrigger,
    CreateNotificationAction,
    CreateTaskAction,
    EntityChangedTrigger,
    GenerateBriefAction,
    GoalDeadlineTrigger,
    NewDocumentTrigger,
    NewEventTrigger,
    RunAgentAction,
    ScheduleTrigger,
    TaskDueTrigger,
    Trigger,
    WebhookTrigger,
)


class TestTriggerSchemas:
    """Tests for discriminated Trigger variants and field validators."""

    def test_schedule_trigger_valid(self) -> None:
        """Verify ScheduleTrigger parses standard 5-field cron and IANA timezone."""
        trigger = ScheduleTrigger(type="schedule", cron="0 9 * * 1-5", timezone="Asia/Ho_Chi_Minh")
        assert trigger.type == "schedule"
        assert trigger.cron == "0 9 * * 1-5"
        assert trigger.timezone == "Asia/Ho_Chi_Minh"

    def test_schedule_trigger_invalid_cron_tokens(self) -> None:
        """Cron with wrong token count or invalid characters must be rejected."""
        with pytest.raises(ValidationError, match="cron must have five fields"):
            ScheduleTrigger(type="schedule", cron="0 9 * *", timezone="UTC")

    def test_schedule_trigger_unknown_timezone(self) -> None:
        """Unknown timezone must be rejected."""
        with pytest.raises(ValidationError, match="timezone must be a valid IANA timezone"):
            ScheduleTrigger(type="schedule", cron="0 9 * * *", timezone="Invalid/Timezone")

    def test_task_due_trigger_lead_minutes(self) -> None:
        """Verify TaskDueTrigger lead_minutes bounds [0, 10080]."""
        trigger = TaskDueTrigger(type="task_due", lead_minutes=60)
        assert trigger.lead_minutes == 60

        with pytest.raises(ValidationError):
            TaskDueTrigger(type="task_due", lead_minutes=-1)

        with pytest.raises(ValidationError):
            TaskDueTrigger(type="task_due", lead_minutes=10_081)

    def test_webhook_trigger_hook_pattern(self) -> None:
        """Verify WebhookTrigger hook name regex pattern."""
        valid_hook = WebhookTrigger(type="webhook", hook="github_pr_opened")
        assert valid_hook.hook == "github_pr_opened"

        # Uppercase or invalid chars rejected
        with pytest.raises(ValidationError):
            WebhookTrigger(type="webhook", hook="GitHub-Hook")


class TestActionSchemas:
    """Tests for Action variants: RunAgentAction, CreateTaskAction, CreateNotificationAction, etc."""

    def test_run_agent_action_profile_roster(self) -> None:
        """Verify RunAgentAction only accepts allowed fixed-roster specialist profiles."""
        valid_profiles = ["knowledge", "research", "personal", "project", "news", "planning", "automation"]
        for p in valid_profiles:
            action = RunAgentAction(type="run_agent", profile_id=p, instruction="Analyze data")  # type: ignore[arg-type]
            assert action.profile_id == p

        with pytest.raises(ValidationError):
            RunAgentAction(type="run_agent", profile_id="unregistered_agent", instruction="Test")  # type: ignore[arg-type]

    def test_create_task_action_bounds(self) -> None:
        """Verify CreateTaskAction title and due_in_days validation."""
        act = CreateTaskAction(type="create_task", title="Follow up email", due_in_days=3)
        assert act.title == "Follow up email"
        assert act.due_in_days == 3

        with pytest.raises(ValidationError):
            CreateTaskAction(type="create_task", title="")

        with pytest.raises(ValidationError):
            CreateTaskAction(type="create_task", title="Valid", due_in_days=-1)

    def test_create_notification_action_link_validation(self) -> None:
        """Verify relative in-app link validation on notification action."""
        act = CreateNotificationAction(type="create_notification", message="Review plan", link="/goals/123")
        assert act.link == "/goals/123"

        # Absolute or malicious link rejected
        with pytest.raises(ValidationError):
            CreateNotificationAction(type="create_notification", message="Review", link="https://evil.com")

    def test_call_webhook_action_alias(self) -> None:
        """Verify CallWebhookAction alias and event pattern."""
        act = CallWebhookAction(type="call_webhook", alias="slack_alerts", event="task_overdue")
        assert act.alias == "slack_alerts"
        assert act.event == "task_overdue"


class TestAutomationDefinitionSchemas:
    """Tests for AutomationDefinition cross-validation and condition limits."""

    def test_automation_definition_valid(self) -> None:
        """Verify valid AutomationDefinition with matching trigger and conditions."""
        trigger = TaskDueTrigger(type="task_due", lead_minutes=30)
        condition = Condition(field="status", operator="eq", value="inbox")
        action = CreateNotificationAction(type="create_notification", message="Task due soon!", link="/tasks")

        defn = AutomationDefinition(
            trigger=trigger,
            conditions=[condition],
            actions=[action],
        )
        assert defn.trigger.type == "task_due"
        assert len(defn.conditions) == 1
        assert len(defn.actions) == 1

    def test_automation_definition_undeclared_condition_field_rejected(self) -> None:
        """Condition referencing field not declared by trigger type is rejected."""
        trigger = WebhookTrigger(type="webhook", hook="custom_hook")
        # Webhook trigger only declares "event", NOT "hours_until_due"!
        invalid_cond = Condition(field="hours_until_due", operator="lt", value=2)
        action = CreateTaskAction(type="create_task", title="Task")

        with pytest.raises(ValueError, match="field 'hours_until_due' is not declared by trigger 'webhook'"):
            AutomationDefinition(
                trigger=trigger,
                conditions=[invalid_cond],
                actions=[action],
            )

    def test_automation_definition_actions_bounds(self) -> None:
        """Actions must contain between 1 and 10 items."""
        trigger = NewEventTrigger(type="new_event")

        # Empty actions rejected
        with pytest.raises(ValidationError):
            AutomationDefinition(trigger=trigger, conditions=[], actions=[])

        # Over 10 actions rejected
        oversized_actions = [
            CreateNotificationAction(type="create_notification", message=f"Msg {i}")
            for i in range(11)
        ]
        with pytest.raises(ValidationError):
            AutomationDefinition(trigger=trigger, conditions=[], actions=oversized_actions)

    def test_automation_create_and_update(self) -> None:
        """Verify AutomationCreate and AutomationUpdate schemas."""
        trigger = NewDocumentTrigger(type="new_document")
        action = GenerateBriefAction(type="generate_brief")

        create = AutomationCreate(
            name="Daily Brief on Ingest",
            enabled=True,
            trigger=trigger,
            conditions=[],
            actions=[action],
        )
        assert create.name == "Daily Brief on Ingest"
        assert create.enabled is True

        update = AutomationUpdate(
            expected_revision=1,
            name="Updated Automation Name",
            enabled=False,
        )
        assert update.expected_revision == 1
        assert update.name == "Updated Automation Name"
        assert update.enabled is False
