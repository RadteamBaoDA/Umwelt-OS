"""Unit tests for automation execution logic, loop guards, approval hashing, and lifecycle finish.

Covers:
- `loop_guard`: detecting potential infinite cascades (task_due -> create_task within lead window,
  entity_changed -> run_agent, webhook -> call_webhook).
- `_approval_hash`: deterministic SHA-256 computation over run_id, ordinal, spec, and destination.
- `_finish`: updating run status, finished_at instant, and failure reason.
"""

from uuid import UUID, uuid4

from modules.automations.execution import (
    _approval_hash,
    _finish,
    loop_guard,
)
from modules.automations.models import AutomationRun


class TestLoopGuard:
    """Tests for cascade and self-feeding loop detection."""

    def test_loop_task_due_create_task_detected(self) -> None:
        trigger = {"type": "task_due", "lead_minutes": 120}
        # due_in_days = 0.05 days = 72 minutes <= 120 minutes -> loop detected
        actions = [{"type": "create_task", "due_in_days": 0.05}]
        assert loop_guard(trigger, actions) == "loop_task_due_create_task"

    def test_loop_task_due_create_task_safe_when_due_later(self) -> None:
        trigger = {"type": "task_due", "lead_minutes": 60}
        # due_in_days = 2 days = 2880 minutes > 60 minutes -> safe
        actions = [{"type": "create_task", "due_in_days": 2.0}]
        assert loop_guard(trigger, actions) is None

    def test_loop_entity_changed_run_agent_detected(self) -> None:
        trigger = {"type": "entity_changed"}
        actions = [{"type": "run_agent", "instructions": "Review entity"}]
        assert loop_guard(trigger, actions) == "loop_entity_changed_run_agent"

    def test_loop_webhook_call_webhook_detected(self) -> None:
        trigger = {"type": "webhook", "event": "external.ping"}
        actions = [{"type": "call_webhook", "destination": "ext_service"}]
        assert loop_guard(trigger, actions) == "loop_webhook_call_webhook"

    def test_safe_combinations_return_none(self) -> None:
        trigger = {"type": "schedule", "cron": "0 9 * * 1"}
        actions = [
            {"type": "create_task", "title": "Weekly review"},
            {"type": "create_notification", "title": "Review time"},
        ]
        assert loop_guard(trigger, actions) is None


class TestApprovalHashAndFinish:
    """Tests for approval hashing and run completion transitions."""

    def test_approval_hash_deterministic(self) -> None:
        run = AutomationRun(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            owner_id=1,
            automation_id=uuid4(),
            revision=1,
            trigger_key="sched:1",
            trigger_type="schedule",
            payload={},
            status="queued",
        )
        spec = {"type": "execute_tool", "tool": "github.create_issue"}
        dest = "github_prod"

        h1 = _approval_hash(run, 0, spec, dest)
        h2 = _approval_hash(run, 0, spec, dest)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex string

        # Changing ordinal or destination must produce different hash
        assert _approval_hash(run, 1, spec, dest) != h1
        assert _approval_hash(run, 0, spec, "different_dest") != h1

    def test_finish_transitions_run_state(self) -> None:
        run = AutomationRun(
            id=uuid4(),
            owner_id=1,
            automation_id=uuid4(),
            revision=1,
            trigger_key="sched:2",
            trigger_type="schedule",
            payload={},
            status="running",
        )
        assert run.finished_at is None

        _finish(run, "succeeded", None)
        assert run.status == "succeeded"
        assert run.finished_at is not None
        assert run.reason is None

        _finish(run, "failed", "Rate limited by upstream")
        assert run.status == "failed"
        assert run.reason == "Rate limited by upstream"
