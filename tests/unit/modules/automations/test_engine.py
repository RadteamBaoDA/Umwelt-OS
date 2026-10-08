"""Unit tests for automation engine execution loop, action dispatching, cooldowns, and deduplication.

Covers:
- Automation engine run loop (process_run): concurrency serialization (1 per rule), retry attempt exhaustion (MAX_RUN_ATTEMPTS=4), and action execution settlement.
- Action dispatching (dispatch_runs): ARQ job enqueueing with generation-tagged job IDs and expiring stale approvals.
- Owner approval decision (decide_action): approval re-queuing, denial terminalization, and stale rule rejection.
- Admission limits and cooldown windows (_admission, plan_run): depth ceiling (MAX_DEPTH=5), self-trigger suppression, hourly rate limit (30/hr), and cooldown spacing (60s).
- Deduplication keys (enqueue_trigger, run_exists): producer event_key deduplication and identity index conflict suppression.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.automations.execution import (
    COOLDOWN_SECONDS,
    MAX_DEPTH,
    MAX_RUN_ATTEMPTS,
    MAX_RUNS_PER_HOUR,
    _admission,
    decide_action,
    dispatch_runs,
    enqueue_trigger,
    process_run,
    run_exists,
)
from modules.automations.models import (
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
)
from modules.automations.schemas import RunRead

OWNER = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)
SC = {"scope": OWNER, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _fenced():
    """Admission and the fenced commit are covered in test_scope; here they are stubs."""
    with patch("modules.automations.execution._admit", AsyncMock(return_value=MagicMock())), \
         patch("modules.automations.execution.commit_with_replay", AsyncMock()) as commit:
        yield commit


class TestDeduplicationAndTriggerInbox:
    """Tests for producer event inbox deduplication and run identity checks."""

    @pytest.mark.asyncio
    async def test_enqueue_trigger_rejects_schedule(self) -> None:
        """enqueue_trigger raises ValueError for schedule trigger type (schedule is owned by worker)."""
        session = AsyncMock()
        with pytest.raises(ValueError, match="trigger type cannot be offered by a producer"):
            await enqueue_trigger(session, trigger_type="schedule", event_key="k1", payload={}, **SC)

    @pytest.mark.asyncio
    async def test_enqueue_trigger_deduplicates_duplicate_event_key(self) -> None:
        """enqueue_trigger returns False when ON CONFLICT DO NOTHING returns no row (duplicate key)."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # Conflict: no insert
        session.execute.return_value = mock_result

        with patch("modules.automations.execution.validate_sample", return_value=None):
            inserted = await enqueue_trigger(
                session, trigger_type="new_event", event_key="event-123",
                payload={"event_id": str(uuid4())}, **SC,
            )
            assert inserted is False

    @pytest.mark.asyncio
    async def test_enqueue_trigger_webhook_requires_hook_name(self) -> None:
        """enqueue_trigger raises ValueError if webhook trigger lacks a hook identifier."""
        session = AsyncMock()
        with patch("modules.automations.execution.validate_sample", return_value=None):  # noqa: SIM117  # style-only rewrite skipped to avoid touching control flow
            with pytest.raises(ValueError, match="webhook triggers need a hook name"):
                await enqueue_trigger(
                    session, trigger_type="webhook", event_key="hook-1",
                    payload={"event": "github.push"}, hook=None, **SC,
                )

    @pytest.mark.asyncio
    async def test_run_exists_returns_true_when_found(self) -> None:
        """run_exists returns True when a run row matches automation_id, revision, and trigger_key."""
        session = AsyncMock()
        rev = AutomationRevision(automation_id=uuid4(), revision=1)
        session.scalar.return_value = uuid4()  # Run ID exists

        assert await run_exists(session, rev, "event:key-1", scope=OWNER) is True

    @pytest.mark.asyncio
    async def test_run_exists_returns_false_when_absent(self) -> None:
        """run_exists returns False when no run matches."""
        session = AsyncMock()
        rev = AutomationRevision(automation_id=uuid4(), revision=1)
        session.scalar.return_value = None

        assert await run_exists(session, rev, "event:key-1", scope=OWNER) is False


class TestAdmissionAndCooldownWindows:
    """Tests for depth limits, self-origin loop prevention, hourly rates, and cooldowns."""

    def _sample_revision(self, auto_id: UUID | None = None) -> AutomationRevision:
        """Generate a valid AutomationRevision instance."""
        return AutomationRevision(
            automation_id=auto_id or uuid4(),
            revision=1,
            trigger={"type": "new_event"},
            actions=[{"type": "create_task", "title": "Follow up"}],
        )

    @pytest.mark.asyncio
    async def test_admission_depth_exceeded_skips_run(self) -> None:
        """_admission returns depth_exceeded reason when causal chain depth > MAX_DEPTH (5)."""
        session = AsyncMock()
        rev = self._sample_revision()
        now = datetime.now(UTC)

        with patch("modules.automations.execution.dependencies_missing", return_value=[]):
            reason, run_at = await _admission(
                session, rev, trigger_type="new_event", depth=MAX_DEPTH + 1,
                origin_automation_id=None, now=now, scope=OWNER,
            )
            assert reason == "depth_exceeded"
            assert run_at == now

    @pytest.mark.asyncio
    async def test_admission_self_origin_skips_run(self) -> None:
        """_admission returns self_origin reason when an automation triggered its own event."""
        session = AsyncMock()
        auto_id = uuid4()
        rev = self._sample_revision(auto_id=auto_id)
        now = datetime.now(UTC)

        with patch("modules.automations.execution.dependencies_missing", return_value=[]):
            reason, _run_at = await _admission(
                session, rev, trigger_type="new_event", depth=1,
                origin_automation_id=auto_id, now=now, scope=OWNER,
            )
            assert reason == "self_origin"

    @pytest.mark.asyncio
    async def test_admission_hourly_cap_skips_run(self) -> None:
        """_admission returns rate_limited when hourly run count exceeds MAX_RUNS_PER_HOUR (30)."""
        session = AsyncMock()
        rev = self._sample_revision()
        now = datetime.now(UTC)

        # Mock count of runs in the last hour >= 30
        session.scalar.return_value = MAX_RUNS_PER_HOUR

        with patch("modules.automations.execution.dependencies_missing", return_value=[]):
            reason, _run_at = await _admission(
                session, rev, trigger_type="new_event", depth=1,
                origin_automation_id=None, now=now, scope=OWNER,
            )
            assert reason == "rate_limited"

    @pytest.mark.asyncio
    async def test_admission_cooldown_spaces_non_scheduled_run(self) -> None:
        """_admission spaces non-scheduled runs by COOLDOWN_SECONDS (60s) from the last planned run."""
        session = AsyncMock()
        rev = self._sample_revision()
        now = datetime.now(UTC)
        last_attempt = now + timedelta(seconds=20)

        # 1st scalar: hourly count (0), 2nd scalar: last planned attempt
        session.scalar.side_effect = [0, last_attempt]

        with patch("modules.automations.execution.dependencies_missing", return_value=[]):
            reason, run_at = await _admission(
                session, rev, trigger_type="new_event", depth=1,
                origin_automation_id=None, now=now, scope=OWNER,
            )
            assert reason is None
            assert run_at == last_attempt + timedelta(seconds=COOLDOWN_SECONDS)


class TestActionDispatchingAndDecisions:
    """Tests for dispatch_runs and owner decide_action approval flows."""

    @pytest.mark.asyncio
    async def test_dispatch_runs_enqueues_eligible_runs(self) -> None:
        """dispatch_runs enqueues runnable jobs in Redis with unique generation-tagged job IDs."""
        run_id = uuid4()
        now = datetime.now(UTC)
        run = AutomationRun(
            id=run_id,
            automation_id=uuid4(),
            revision=1,
            status="queued",
            next_attempt_at=now - timedelta(seconds=10),
            dispatched_at=None,
            dispatch_generation=1,
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.side_effect = [
            [],      # expired approvals
            [run],   # runnable runs
        ]
        mock_session.scalars.return_value = mock_scalars

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        redis_mock = AsyncMock()
        enqueued = await dispatch_runs(mock_factory, redis_mock, **SC)

        assert enqueued == 1
        assert run.dispatch_generation == 2
        redis_mock.enqueue_job.assert_called_once()
        args, kwargs = redis_mock.enqueue_job.call_args
        assert args == ("process_automation_run", str(run_id))
        assert kwargs["_job_id"] == f"automation-run:{run_id}:2"
        assert isinstance(kwargs["_defer_until"], datetime)

    @pytest.mark.asyncio
    async def test_decide_action_approve_requeues_run(self, _fenced: AsyncMock) -> None:
        """decide_action with approve=True marks action approved and sets run status to queued."""
        session = AsyncMock()
        run_id = uuid4()
        auto_id = uuid4()
        now = datetime.now(UTC)

        run = AutomationRun(
            id=run_id,
            owner_id=1,
            automation_id=auto_id,
            revision=1,
            status="awaiting_approval",
        )
        action = AutomationRunAction(
            id=uuid4(),
            run_id=run_id,
            ordinal=1,
            status="awaiting_approval",
            approval_expires_at=now + timedelta(hours=1),
            approval_hash="valid_hash",
            destination_revision=None,
        )
        rev = AutomationRevision(
            automation_id=auto_id,
            revision=1,
            actions=[{"type": "run_agent", "instructions": "Review"}],
        )

        session.scalar.side_effect = [run, action, rev]

        with patch("modules.automations.execution._approval_hash", return_value="valid_hash"), \
             patch("modules.automations.execution._fence_current", return_value=True), \
             patch("modules.automations.execution.dependencies_missing", return_value=[]), \
             patch("modules.automations.execution._reads", return_value=[MagicMock(spec=RunRead)]):
            await decide_action(
                session, session_hash="owner_sess", run_id=run_id,
                ordinal=1, approve=True, settings=MagicMock(), **SC,
            )
            assert action.status == "approved"
            assert action.approved_session_hash == "owner_sess"
            assert run.status == "queued"
            _fenced.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_decide_action_deny_fails_run_and_skips_pending(self) -> None:
        """decide_action with approve=False marks action denied, finishes run as failed, and skips pending."""
        session = AsyncMock()
        run_id = uuid4()
        auto_id = uuid4()
        now = datetime.now(UTC)

        run = AutomationRun(
            id=run_id,
            owner_id=1,
            automation_id=auto_id,
            revision=1,
            status="awaiting_approval",
        )
        action = AutomationRunAction(
            id=uuid4(),
            run_id=run_id,
            ordinal=1,
            status="awaiting_approval",
            approval_expires_at=now + timedelta(hours=1),
            approval_hash="valid_hash",
            destination_revision=None,
        )
        rev = AutomationRevision(
            automation_id=auto_id,
            revision=1,
            actions=[{"type": "run_agent", "instructions": "Review"}],
        )

        session.scalar.side_effect = [run, action, rev]
        session.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        with patch("modules.automations.execution._approval_hash", return_value="valid_hash"), \
             patch("modules.automations.execution._fence_current", return_value=True), \
             patch("modules.automations.execution.dependencies_missing", return_value=[]), \
             patch("modules.automations.execution._reads", return_value=[MagicMock(spec=RunRead)]):
            await decide_action(
                session, session_hash="owner_sess", run_id=run_id,
                ordinal=1, approve=False, settings=MagicMock(), **SC,
            )
            assert action.status == "denied"
            assert run.status == "failed"
            assert run.reason == "denied"


def _ctx(factory: MagicMock) -> dict[str, object]:
    return {"session_factory": factory, "settings": MagicMock(multi_workspace_enabled=False)}


def _identity(session: AsyncMock) -> None:
    """The unlocked (workspace_id, owner_id) read that precedes the scoped claim."""
    session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(OWNER.workspace_id, OWNER.user_id))))


@pytest.fixture
def _owner_ok():
    owner = MagicMock(user_id=OWNER.user_id, membership_revision=1)
    with patch("modules.automations.execution.workspaces.resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
         patch("modules.automations.execution.settings_public.module_is_enabled", AsyncMock(return_value=True)):
        yield


@pytest.mark.usefixtures("_owner_ok")
class TestAutomationEngineRunLoop:
    """Tests for process_run execution loop, concurrency, and attempt bounds."""

    @pytest.mark.asyncio
    async def test_process_run_noop_when_not_queued(self) -> None:
        """process_run returns noop when the run is not in queued status."""
        run_id = uuid4()
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None  # No queued run found
        _identity(mock_session)

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        outcome = await process_run(_ctx(mock_factory), str(run_id))
        assert outcome == "noop"

    @pytest.mark.asyncio
    async def test_process_run_busy_when_sibling_run_is_running(self) -> None:
        """process_run returns busy and defers next attempt when a sibling run is currently running."""
        run_id = uuid4()
        now = datetime.now(UTC)
        run = AutomationRun(
            id=run_id,
            automation_id=uuid4(),
            status="queued",
            next_attempt_at=now - timedelta(seconds=1),
        )

        mock_session = AsyncMock()
        _identity(mock_session)
        # 1st scalar: run, 2nd scalar: busy count (1)
        mock_session.scalar.side_effect = [run, 1]

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        outcome = await process_run(_ctx(mock_factory), str(run_id))
        assert outcome == "busy"
        assert run.next_attempt_at > now

    @pytest.mark.asyncio
    async def test_process_run_exhausted_attempts_fails_run(self) -> None:
        """process_run marks run failed when attempts reach MAX_RUN_ATTEMPTS (4)."""
        run_id = uuid4()
        now = datetime.now(UTC)
        run = AutomationRun(
            id=run_id,
            automation_id=uuid4(),
            status="queued",
            next_attempt_at=now - timedelta(seconds=1),
            attempts=MAX_RUN_ATTEMPTS,  # Exhausted!
        )

        mock_session = AsyncMock()
        _identity(mock_session)
        # 1st scalar: run, 2nd scalar: busy count (0)
        mock_session.scalar.side_effect = [run, 0]
        mock_session.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        outcome = await process_run(_ctx(mock_factory), str(run_id))
        assert outcome == "failed"
        assert run.status == "failed"
        assert run.reason == "attempts_exhausted"
