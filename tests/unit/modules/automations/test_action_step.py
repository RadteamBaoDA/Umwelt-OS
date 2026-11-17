"""Characterization of ``_action_step``: row-state handling, durable pauses and dispatch ordering."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from uuid import uuid4

import pytest

from modules.automations import execution
from tests.unit.split_patching import patch_split

RUN_ID = uuid4()
SCOPE = SimpleNamespace(workspace_id=uuid4(), actor_user_id=1)


class FakeSession:
    """Serve the action row then its revision, ignoring every other statement."""

    def __init__(self, row: Any, rev: Any) -> None:
        """Queue the two ``scalar`` results."""
        self.queue = [row, rev]

    async def scalar(self, _statement: Any) -> Any:
        """Return the next queued row."""
        return self.queue.pop(0)

    async def __aenter__(self) -> Self:
        """Enter the session scope."""
        return self

    async def __aexit__(self, *_a: object) -> None:
        """Leave the session scope."""


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str = "create_task",
    state: str = "pending",
    **over: Any,
) -> tuple[Any, list[str], dict[str, Any], Any]:
    """Patch every collaborator with an event-recording spy and return the call context."""
    events: list[str] = []
    row = SimpleNamespace(
        status=state,
        error_code=None,
        attempts=0,
        destination_revision=None,
        approved_session_hash="sess",
        approval_hash=None,
        approval_expires_at=None,
    )
    spec = {"type": kind, "alias": "hook"}
    rev = SimpleNamespace(actions=[spec], name="Rule")
    run = SimpleNamespace(id=RUN_ID, automation_id=uuid4(), revision=1)
    flags: dict[str, Any] = {
        "evidence": True,
        "fence": True,
        "modules": True,
        "dest_stale": False,
        "destination": "rev-1",
    }
    flags.update(over)

    async def spy(name: str, result: Any = None, *_a: Any, **_k: Any) -> Any:
        """Record an awaited collaborator call."""
        events.append(name)
        return result

    async def admit(*_a: Any, **_k: Any) -> str:
        """Return a stand-in fence."""
        events.append("admit")
        return "fence"

    async def locked_run(*_a: Any, **_k: Any) -> Any:
        """Return the locked run."""
        return run

    async def evidence(*_a: Any, **_k: Any) -> bool:
        """Answer the evidence-current probe."""
        return flags["evidence"]

    async def fence_current(*_a: Any, **_k: Any) -> bool:
        """Answer the revision fence probe."""
        return flags["fence"]

    async def modules(*_a: Any, **_k: Any) -> bool:
        """Answer the owner-module probe."""
        return flags["modules"]

    patch_split(monkeypatch, "modules.automations", "_admit", admit, facade="execution")
    patch_split(monkeypatch, "modules.automations", "_locked_run", locked_run, facade="execution")
    patch_split(
        monkeypatch, "modules.automations", "_run_evidence_current", evidence, facade="execution"
    )
    patch_split(
        monkeypatch, "modules.automations", "_fence_current", fence_current, facade="execution"
    )
    patch_split(
        monkeypatch, "modules.automations", "_action_modules_enabled", modules, facade="execution"
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_destination_stale",
        lambda *_a: flags["dest_stale"],
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "webhook_profile_revision",
        lambda *_a: flags["destination"],
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_approval_hash",
        lambda *_a: "approval-hash",
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_finish",
        lambda r, status, reason: events.append(f"finish:{status}:{reason}"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_skip_pending",
        lambda *_a: spy("skip_pending"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_commit",
        lambda *_a, **_k: spy("commit"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "emit",
        lambda *_a, **_k: spy("emit"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_in_database_action",
        lambda *_a, **_k: spy("in_database", "in-db"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_generate_brief",
        lambda *_a, **_k: spy("brief", "brief-done"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_start_agent",
        lambda *a, **_k: spy("start_agent:" + str(a[4]), "agent-done"),
        facade="execution",
    )
    patch_split(
        monkeypatch,
        "modules.automations",
        "_send_webhook",
        lambda *a, **_k: spy("send_webhook:" + str(a[5]), "hook-done"),
        facade="execution",
    )
    ctx = {
        "session_factory": lambda: FakeSession(row, rev),
        "settings": SimpleNamespace(approval_expiry_hours=24),
    }
    return row, events, flags, ctx


async def _step(ctx: Any) -> str:
    """Advance action 1 of the shared run."""
    return await execution._action_step(ctx, RUN_ID, 1, scope=SCOPE, multi_workspace_enabled=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("denied", "failed"),
        ("skipped", "failed"),
        ("awaiting_approval", "awaiting_approval"),
    ],
)
async def test_terminal_and_waiting_states(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: str
) -> None:
    """Settled rows report their outcome without committing; awaiting approval stays parked."""
    row, events, _flags, ctx = _setup(monkeypatch, state=state)
    assert await _step(ctx) == expected
    assert "commit" not in events and row.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "result", "row_state", "code"),
    [
        ("in_flight", "requires_review", "requires_review", "document_evidence_unavailable"),
        ("pending", "dropped", "skipped", "document_evidence_unavailable"),
    ],
)
async def test_missing_evidence_settles_run(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    result: str,
    row_state: str,
    code: str,
) -> None:
    """Revoked evidence keeps uncertainty for dispatched rows and drops untouched ones."""
    row, events, _flags, ctx = _setup(monkeypatch, state=state, evidence=False)
    assert await _step(ctx) == result
    assert (row.status, row.error_code) == (row_state, code)
    assert events == ["admit", f"finish:{result}:{code}", "skip_pending", "commit"]


@pytest.mark.asyncio
async def test_ambiguous_restart_requires_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-agent in-flight row after a restart is review-only and never replayed."""
    row, events, _flags, ctx = _setup(monkeypatch, kind="call_webhook", state="in_flight")
    assert await _step(ctx) == "requires_review"
    assert (row.status, row.error_code) == ("requires_review", "ambiguous_after_restart")
    assert events[-1] == "commit"


@pytest.mark.asyncio
async def test_stale_revision_destination_and_paused_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence, module and destination drift drop or pause the action without dispatching."""
    row, events, _f, ctx = _setup(monkeypatch, fence=False)
    assert await _step(ctx) == "dropped" and (row.status, row.error_code) == (
        "skipped",
        "stale_revision",
    )
    row, events, _f, ctx = _setup(monkeypatch, modules=False)
    assert await _step(ctx) == "paused" and row.status == "pending" and "commit" not in events
    row, events, _f, ctx = _setup(
        monkeypatch, kind="call_webhook", state="approved", dest_stale=True
    )
    assert await _step(ctx) == "dropped" and row.error_code == "stale_destination"


@pytest.mark.asyncio
async def test_webhook_needs_approval_then_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending webhook binds its destination revision, hashes the request and notifies the owner."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook")
    assert await _step(ctx) == "awaiting_approval"
    assert (row.status, row.destination_revision, row.approval_hash) == (
        "awaiting_approval",
        "rev-1",
        "approval-hash",
    )
    assert row.approval_expires_at is not None and events[-2:] == ["emit", "commit"]


@pytest.mark.asyncio
async def test_webhook_without_profile_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing deployment profile fails the action instead of asking for approval."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook", destination=None)
    assert (
        await _step(ctx) == "failed"
        and row.error_code == "webhook_unavailable"
        and "emit" not in events
    )


@pytest.mark.asyncio
async def test_in_database_actions_run_inside_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification and task actions count an attempt and are delegated with the open session."""
    row, events, _f, ctx = _setup(monkeypatch, kind="create_task")
    assert await _step(ctx) == "in-db" and row.attempts == 1 and events == ["admit", "in_database"]


@pytest.mark.asyncio
async def test_generate_brief_commits_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brief generation releases the locks first because it commits internally."""
    row, events, _f, ctx = _setup(monkeypatch, kind="generate_brief")
    assert (
        await _step(ctx) == "brief-done"
        and row.attempts == 1
        and events == ["admit", "commit", "brief"]
    )


@pytest.mark.asyncio
async def test_run_agent_marks_in_flight_then_starts_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of no return is committed before the idempotent agent start, keeping the digest."""
    row, events, _f, ctx = _setup(monkeypatch, kind="run_agent", state="in_flight")
    assert await _step(ctx) == "agent-done"
    assert row.status == "in_flight" and row.approved_session_hash == "sess"
    assert events == ["admit", "commit", "start_agent:sess"]


@pytest.mark.asyncio
async def test_approved_webhook_clears_digest_and_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved webhook drops the stored session digest before the post-commit send."""
    row, events, _f, ctx = _setup(monkeypatch, kind="call_webhook", state="approved")
    row.destination_revision = "rev-1"
    assert await _step(ctx) == "hook-done"
    assert row.approved_session_hash is None and row.status == "in_flight"
    assert events == ["admit", "commit", "send_webhook:rev-1"]
