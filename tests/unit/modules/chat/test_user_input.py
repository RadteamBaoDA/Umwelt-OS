"""Unit tests for the parked-run user input service (fake session and clock, no database)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.chat import user_input as ui
from modules.chat.generative_ui.tool_specs import (
    INPUT_DECLINE,
    RequestUserInputArgs,
    build_chat_tool_specs,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
WS = uuid4()
SLOT = {"id": "s1", "label": "Tue", "date": "2026-10-20", "start_time": "10:00", "end_time": "10:30", "available": True}
SLOT_OFF = {**SLOT, "id": "s2", "available": False}


class FakeSession:
    """Just enough AsyncSession for user_input: one run, appended events, commit counters."""

    def __init__(self, run: Any) -> None:
        self.run, self.events, self.commits, self.rollbacks, self.heartbeats = run, [], 0, 0, 0

    async def scalar(self, stmt: Any) -> Any:
        text = str(stmt)
        if "max(" in text:
            return max((e.seq for e in self.events), default=0)
        if "FROM chat_conversations" in text:
            return SimpleNamespace(id="c")
        return self.run

    async def scalars(self, stmt: Any) -> list[Any]:
        return [self.run.id] if self.run.status == "awaiting_input" else []

    async def execute(self, stmt: Any) -> None:
        self.heartbeats += 1

    def add(self, event: Any) -> None:
        self.events.append(event)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


def _run(status: str = "streaming") -> Any:
    return SimpleNamespace(
        id=uuid4(), conversation_id=uuid4(), status=status, input_request=None, workspace_id=WS, actor_user_id=2,
        updated_at=T0, completed_at=None, error_code=None, error_message=None,
    )


def _args(kind: str = "time_slots") -> RequestUserInputArgs:
    if kind == "time_slots":
        return RequestUserInputArgs.model_validate({"kind": kind, "prompt": "When?", "slots": [SLOT, SLOT_OFF]})
    if kind == "choice":
        return RequestUserInputArgs.model_validate({
            "kind": kind, "prompt": "Which?", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
    return RequestUserInputArgs.model_validate({"kind": kind, "prompt": "Name?"})


@pytest.fixture(autouse=True)
def _no_privacy_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "lock_export_privacy", AsyncMock())


async def _parked(kind: str = "time_slots") -> tuple[FakeSession, Any, str]:
    run = _run()
    session = FakeSession(run)
    payload = await ui.create_input_request(session, run, _args(kind), ttl=timedelta(minutes=5), now=T0)  # type: ignore[arg-type]
    return session, run, payload["request_id"]


async def _submit(session: FakeSession, rid: str, answer: str, **kw: Any) -> str:
    base: dict[str, Any] = {"workspace_id": WS, "actor_user_id": 2, "now": T0 + timedelta(minutes=1)}
    return await ui.submit_input(
        session, response_id=session.run.id, request_id=rid, answer=answer, **{**base, **kw},  # type: ignore[arg-type]
    )


async def test_create_parks_run_and_event_has_no_answer_state() -> None:
    session, run, rid = await _parked()
    assert run.status == "awaiting_input" and run.input_request["state"] == "pending"
    event = session.events[0]
    assert (event.event_type, event.seq, event.event_id) == ("message.input.request", 1, f"{run.id}:1")
    assert set(event.data) == {"request_id", "kind", "prompt", "options", "slots", "expires_at"}
    assert event.data["request_id"] == rid and "answer" not in event.data and "state" not in event.data
    assert event.data["expires_at"] == (T0 + timedelta(minutes=5)).isoformat()


async def test_create_requires_streaming_run() -> None:
    run = _run("completed")
    with pytest.raises(ui.InputRequestError) as err:
        await ui.create_input_request(FakeSession(run), run, _args(), ttl=timedelta(minutes=1))  # type: ignore[arg-type]
    assert err.value.status_code == 409


async def test_submit_slot_resumes_run_and_returns_tool_result() -> None:
    session, run, rid = await _parked()
    result = json.loads(await _submit(session, rid, "s1"))
    assert result == {"status": "answered", "kind": "time_slots", "answer": "s1", "label": "Tue (2026-10-20 10:00-10:30)"}
    assert run.status == "streaming"  # stays in the active set: the 409 rule has no gap
    resolved = session.events[-1]
    assert resolved.event_type == "message.input.resolved" and resolved.seq == 2
    assert resolved.data["outcome"] == "answered"


async def test_decline_choice_and_text_answers() -> None:
    session, _, rid = await _parked("time_slots")
    assert json.loads(await _submit(session, rid, INPUT_DECLINE)) == {"status": "declined", "kind": "time_slots"}
    session, _, rid = await _parked("choice")
    assert json.loads(await _submit(session, rid, "b"))["label"] == "B"
    session, _, rid = await _parked("text")
    assert json.loads(await _submit(session, rid, "  Ada  "))["answer"] == "Ada"


@pytest.mark.parametrize(("kind", "answer"), [
    ("time_slots", "nope"), ("time_slots", "s2"), ("choice", "z"), ("text", "   "), ("text", "x" * 2001),
])
async def test_invalid_answers_rejected_without_state_change(kind: str, answer: str) -> None:
    session, run, rid = await _parked(kind)
    with pytest.raises(ui.InputRequestError) as err:
        await _submit(session, rid, answer)
    assert (err.value.status_code, err.value.code) == (422, "invalid_answer")
    assert run.status == "awaiting_input" and len(session.events) == 1


async def test_wrong_request_id_and_foreign_owner_are_404() -> None:
    session, run, rid = await _parked()
    for kw, request_id in (({}, "other"), ({"actor_user_id": 9}, rid), ({"workspace_id": uuid4()}, rid)):
        with pytest.raises(ui.InputRequestError) as err:
            await _submit(session, request_id, "s1", **kw)
        assert err.value.status_code == 404
    assert run.status == "awaiting_input"


async def test_double_submit_and_submit_after_cancel_are_409() -> None:
    session, run, rid = await _parked()
    await _submit(session, rid, "s1")
    with pytest.raises(ui.InputRequestError) as err:
        await _submit(session, rid, "s1")
    assert (err.value.status_code, err.value.code) == (409, "not_awaiting")
    session, run, rid = await _parked()
    run.status = "cancelled"
    with pytest.raises(ui.InputRequestError) as err:
        await _submit(session, rid, "s1")
    assert err.value.code == "not_awaiting"


async def test_submit_after_expiry_cancels_run() -> None:
    session, run, rid = await _parked()
    with pytest.raises(ui.InputRequestError) as err:
        await _submit(session, rid, "s1", now=T0 + timedelta(minutes=6))
    assert (err.value.status_code, err.value.code) == (409, "expired")
    assert run.status == "cancelled" and run.input_request["state"] == "expired"
    assert [e.event_type for e in session.events[1:]] == ["message.input.resolved", "status"]
    assert session.events[-1].data == {"status": "cancelled"}


async def test_expire_stale_requests_cancels_expired_and_fails_dead_worker() -> None:
    session, run, _rid = await _parked()
    assert await ui.expire_stale_requests(session, now=T0 + timedelta(minutes=1)) == 0  # type: ignore[arg-type]
    run.updated_at = T0 + timedelta(minutes=1)
    assert await ui.expire_stale_requests(session, now=T0 + timedelta(minutes=4)) == 1  # type: ignore[arg-type]
    assert run.status == "failed" and run.error_code == "WorkerLost"
    session, run, _rid = await _parked()
    assert await ui.expire_stale_requests(session, now=T0 + timedelta(minutes=6)) == 1  # type: ignore[arg-type]
    assert run.status == "cancelled" and session.commits == 1


class Clock:
    """Fake wall clock: `sleep` advances it and runs an optional hook (the 'other actor')."""

    def __init__(self, hook: Any = None) -> None:
        self.t, self.hook, self.sleeps = T0, hook, 0

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.sleeps += 1
        if self.hook:
            await self.hook(self)


async def _wait(session: FakeSession, rid: str, clock: Clock, **kw: Any) -> ui.InputOutcome:
    return await ui.await_user_input(
        lambda: session, session.run.id, rid, now=clock.now, sleep=clock.sleep, **kw,  # type: ignore[arg-type]
    )


async def test_await_returns_answer_submitted_while_parked_and_heartbeats() -> None:
    session, run, rid = await _parked()

    async def answer_after_three_polls(clock: Clock) -> None:
        if clock.sleeps == 3:
            await _submit(session, rid, "s1", now=clock.t)

    out = await _wait(session, rid, Clock(answer_after_three_polls), poll_interval=1.0, heartbeat_interval=2.0)
    assert out.outcome == "answered" and json.loads(out.tool_result)["answer"] == "s1"
    assert session.heartbeats >= 1 and run.status == "streaming"


async def test_await_expires_run_at_deadline() -> None:
    session, run, rid = await _parked()
    out = await _wait(session, rid, Clock(), poll_interval=60.0)
    assert out.outcome == "expired" and json.loads(out.tool_result) == {"status": "expired"}
    assert run.status == "cancelled" and session.commits >= 1


async def test_await_reports_cancel_from_cancel_route() -> None:
    session, run, rid = await _parked()

    async def cancel(clock: Clock) -> None:
        run.status = "cancelled"

    out = await _wait(session, rid, Clock(cancel))
    assert out.outcome == "cancelled"


async def test_await_declined_outcome() -> None:
    session, _, rid = await _parked()
    await _submit(session, rid, INPUT_DECLINE)
    assert (await _wait(session, rid, Clock())).outcome == "declined"


def test_ttl_clamped_to_worker_job_budget() -> None:
    assert ui.resolve_ttl(900) == timedelta(seconds=ui.MAX_PARK_SECONDS)
    assert ui.resolve_ttl(60) == timedelta(seconds=60)


@pytest.mark.parametrize("payload", [
    {"kind": "time_slots", "prompt": "p"},
    {"kind": "choice", "prompt": "p", "options": [{"id": "a", "label": "A"}]},
    {"kind": "choice", "prompt": "p", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]},
    {"kind": "choice", "prompt": "p", "options": [{"id": INPUT_DECLINE, "label": "A"}, {"id": "b", "label": "B"}]},
    {"kind": "text", "prompt": "p", "options": [{"id": "a", "label": "A"}]},
    {"kind": "text", "prompt": ""},
])
def test_args_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RequestUserInputArgs.model_validate(payload)


def test_tool_spec_is_opt_in() -> None:
    def names(**kw: bool) -> set[str]:
        return {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False, **kw)}

    assert "request_user_input" not in names()
    assert "request_user_input" in names(include_user_input=True)
