"""Characterization of ``_account_segment``: usage folding, effect settlement and status decisions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.agents import worker

RUN_ID = uuid4()


class FakeSession:
    """Serve the locked run, then approvals, then in-flight effects; record execute/commit order."""

    def __init__(
        self, row: Any, approvals: list[Any], effects: list[Any], events: list[str]
    ) -> None:
        """Queue the three result sets."""
        self.row, self.sets, self.events = row, [approvals, effects], events

    async def scalar(self, _statement: Any) -> Any:
        """Return the locked run row."""
        self.events.append("lock_run")
        return self.row

    async def scalars(self, _statement: Any) -> Any:
        """Return the next result set."""
        self.events.append("select_set")
        return SimpleNamespace(all=lambda: self.sets.pop(0))

    async def execute(self, _statement: Any) -> None:
        """Record the started-call update."""
        self.events.append("fail_started_calls")

    async def commit(self) -> None:
        """Record the commit."""
        self.events.append("commit")

    async def __aenter__(self) -> Self:
        """Enter the session scope."""
        return self

    async def __aexit__(self, *_a: object) -> None:
        """Leave the session scope."""


def _row(**over: Any) -> SimpleNamespace:
    """Build a running AgentRun double claimed by generation 1."""
    values: dict[str, Any] = {
        "claim_generation": 1,
        "status": "running",
        "active_seconds": 10,
        "claim_started_at": "t",
        "updated_at": None,
        "evidence_revoked": False,
        "cancel_requested": False,
        "token_usage": None,
        "token_usage_unknown": False,
        "source_fences": None,
        "activities": [],
        "checkpoint_thread_id": "thread",
        "dispatch_generation": 1,
        "completed_at": None,
        "error_code": None,
        "owner_id": 1,
        "auth_session_hash": "h",
        "id": RUN_ID,
    }
    values.update(over)
    return SimpleNamespace(**values)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    row: Any,
    *,
    approvals: list[Any] | None = None,
    effects: list[Any] | None = None,
    alive: bool = True,
) -> tuple[Any, list[str], AsyncMock]:
    """Wire the fake session, checkpoint deletion, liveness probes and publisher."""
    events: list[str] = []
    session = FakeSession(row, approvals or [], effects or [], events)
    context = SimpleNamespace(
        run_id=RUN_ID,
        scope=SimpleNamespace(workspace_id=uuid4()),
        claim_generation=1,
        elapsed=lambda: 2.2,
        unobservable_model_usage=False,
        multi_workspace_enabled=False,
    )

    async def delete_checkpoints(_session: Any, thread: str) -> None:
        """Record checkpoint erasure."""
        events.append(f"delete_checkpoints:{thread}")

    publish = AsyncMock()
    monkeypatch.setattr(worker, "_delete_checkpoints", delete_checkpoints)
    monkeypatch.setattr(worker, "publish_agent_activity_safely", publish)
    monkeypatch.setattr(
        "core.auth.public.revalidate_account_session", AsyncMock(return_value=alive)
    )
    monkeypatch.setattr("modules.chat.public.has_live_agent_run_link", AsyncMock(return_value=True))
    return (lambda: session, context), events, publish


async def _run(setup: Any, **kw: Any) -> None:
    """Invoke the accounting step with the prepared factory and context."""
    factory, context = setup
    await worker._account_segment(factory, context, **kw)


@pytest.mark.asyncio
async def test_stale_claim_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row claimed by another generation (or not running) is left untouched and unpublished."""
    row = _row(claim_generation=2)
    setup, events, publish = _setup(monkeypatch, row)
    await _run(setup, state=None, outcome="continue")
    assert events == ["lock_run"] and row.active_seconds == 10
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_requeues_and_folds_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A continuing segment adds active seconds, folds usage/fences, settles effects and requeues."""
    effect = SimpleNamespace(action_id="a1", state="in_flight", payload={"x": 1})
    approval = SimpleNamespace(action_id="a1", status="approved", resolved_at=None)
    row = _row()
    setup, events, publish = _setup(monkeypatch, row, approvals=[approval], effects=[effect])
    state = {"token_usage": 5, "token_usage_unknown": True, "source_fences": {"records": []}}
    await _run(setup, state=state, outcome="continue")
    assert events == ["lock_run", "select_set", "select_set", "fail_started_calls", "commit"]
    assert (row.active_seconds, row.claim_started_at, row.token_usage, row.token_usage_unknown) == (
        13,
        None,
        5,
        True,
    )
    assert row.source_fences == {"records": []}
    assert (effect.state, effect.payload, approval.status) == (
        "requires_review",
        None,
        "requires_review",
    )
    assert (row.status, row.dispatch_generation, row.activities[-1]["status"]) == (
        "queued",
        2,
        "queued",
    )
    assert publish.await_args.kwargs["status"] == "queued"


@pytest.mark.asyncio
async def test_revoked_evidence_cancels_and_ignores_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoked evidence discards graph usage, cancels the run and erases the checkpoint."""
    row = _row(evidence_revoked=True)
    setup, events, _publish = _setup(monkeypatch, row)
    await _run(setup, state={"token_usage": 9}, outcome="continue")
    assert row.token_usage is None and (row.status, row.cancel_requested) == ("cancelled", True)
    assert "delete_checkpoints:thread" in events and events[-1] == "commit"


@pytest.mark.asyncio
async def test_waiting_approval_parks_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live owner session and Chat link keep the run waiting for approval."""
    row = _row()
    setup, events, _publish = _setup(monkeypatch, row)
    await _run(setup, state={"waiting_approval": True}, outcome="continue")
    assert row.status == "waiting_approval" and not any(e.startswith("delete") for e in events)


@pytest.mark.asyncio
async def test_waiting_approval_cancels_when_session_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead session cancels the run, scrubs pending approvals and erases the checkpoint."""
    pending = SimpleNamespace(
        action_id="p",
        status="pending",
        resolved_at=None,
        arguments={"a": 1},
        source_fences={"b": 1},
    )
    done = SimpleNamespace(
        action_id="d",
        status="approved",
        resolved_at=None,
        arguments={"a": 1},
        source_fences={"b": 1},
    )
    row = _row()
    setup, events, _publish = _setup(monkeypatch, row, approvals=[pending, done], alive=False)
    await _run(setup, state={"waiting_approval": True}, outcome="continue")
    assert (row.status, row.cancel_requested) == (
        "cancelled",
        True,
    ) and row.completed_at is not None
    assert (pending.status, pending.arguments, pending.source_fences) == ("cancelled", None, {})
    assert (done.status, done.arguments) == ("approved", {"a": 1})
    assert "delete_checkpoints:thread" in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "outcome", "code", "unknown"),
    [
        (10, "segment_timeout", "segment_timeout", True),
        (10, "run_limit_exceeded", "run_limit_exceeded", False),
        (299, "continue", "active_time_limit", False),
    ],
)
async def test_failure_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    active: int,
    outcome: str,
    code: str,
    unknown: bool,
) -> None:
    """Non-continuing outcomes (or an exhausted budget) fail the run with the right code."""
    row = _row(active_seconds=active)
    setup, events, publish = _setup(monkeypatch, row)
    await _run(setup, state=None, outcome=outcome)
    assert (row.status, row.error_code, row.token_usage_unknown) == ("failed", code, unknown)
    assert row.completed_at is not None and "delete_checkpoints:thread" in events
    assert publish.await_args.kwargs["status"] == "failed"
