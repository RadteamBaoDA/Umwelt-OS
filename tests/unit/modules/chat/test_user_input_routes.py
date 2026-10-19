"""Route-level tests for the user input endpoint, cancel in awaiting_input, and the SSE event shape."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from modules.chat import routes, worker
from modules.chat import user_input as ui
from modules.chat.schemas import SubmitInputRequest
from modules.chat.stream import format_sse_event

WS = uuid4()


def _session() -> MagicMock:
    return MagicMock(commit=AsyncMock(), rollback=AsyncMock())


def _owner() -> Any:
    return SimpleNamespace(owner_id=2)


@pytest.fixture(autouse=True)
def _scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "owner_default_scope", AsyncMock(return_value=SimpleNamespace(workspace_id=WS)))


def test_input_route_is_registered_with_write_auth() -> None:
    route = next(r for r in routes.router.routes if getattr(r, "path", "") == "/api/v1/responses/{response_id}/input")
    assert route.methods == {"POST"}  # type: ignore[attr-defined]
    calls = {d.call for d in route.dependant.dependencies}  # type: ignore[attr-defined]
    assert routes.OwnerWrite.__metadata__[0].dependency in calls  # CSRF-checked account write


def test_body_is_strict() -> None:
    SubmitInputRequest(request_id="r", answer="a")
    for bad in ({"request_id": "r", "answer": "a", "x": 1}, {"request_id": "r", "answer": 3}, {"request_id": "", "answer": "a"}):
        with pytest.raises(ValidationError):
            SubmitInputRequest.model_validate(bad)


async def test_submit_success_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    submit = AsyncMock(return_value="{}")
    monkeypatch.setattr(routes, "submit_input", submit)
    session, rid = _session(), uuid4()
    out = await routes.submit_response_input(rid, SubmitInputRequest(request_id="r1", answer="s1"), session, _owner())
    assert (out.response_id, out.request_id, out.status) == (rid, "r1", "streaming")
    assert submit.await_args.kwargs["workspace_id"] == WS and submit.await_args.kwargs["actor_user_id"] == 2
    session.commit.assert_awaited_once()


@pytest.mark.parametrize(("status", "code", "commits"), [(404, "not_found", 0), (409, "not_awaiting", 0), (422, "invalid_answer", 0), (409, "expired", 1)])
async def test_submit_errors_map_to_http(monkeypatch: pytest.MonkeyPatch, status: int, code: str, commits: int) -> None:
    monkeypatch.setattr(routes, "submit_input", AsyncMock(side_effect=ui.InputRequestError(status, code)))
    session = _session()
    with pytest.raises(HTTPException) as exc:
        await routes.submit_response_input(uuid4(), SubmitInputRequest(request_id="r", answer="a"), session, _owner())
    assert (exc.value.status_code, exc.value.detail) == (status, code)
    assert session.commit.await_count == commits  # only the expiry is persisted


def test_awaiting_input_is_active_everywhere_routes_check() -> None:
    assert "awaiting_input" in routes.ACTIVE_RUN_STATUSES  # 409 rule, SSE open, detail active_response_id
    assert "awaiting_input" not in ("completed", "cancelled", "failed")


async def test_cancel_helpers_accept_awaiting_input() -> None:
    run = SimpleNamespace(id=uuid4(), status="awaiting_input")
    session = MagicMock(scalar=AsyncMock(return_value=4), add=MagicMock())
    assert await worker._cancel_response_locked(session, run) == 5  # type: ignore[arg-type]
    assert run.status == "cancelled"
    assert session.add.call_args.args[0].data == {"status": "cancelled"}


async def test_poll_probe_treats_awaiting_input_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle poll of a parked run must stay lock-free (the probe only returns early for terminal runs)."""
    run = SimpleNamespace(status="awaiting_input", conversation_id=uuid4(), actor_user_id=2, workspace_id=WS, retrieval_context={})
    session = MagicMock(scalar=AsyncMock(side_effect=[run, None, None]))
    assert await routes._poll_needs_lock(session, uuid4(), 0, "h") is True  # reached parent lookup: not short-circuited
    assert session.scalar.await_count == 3


def test_sse_event_shape() -> None:
    payload = {"request_id": "r", "kind": "choice", "prompt": "p", "options": [], "slots": [], "expires_at": "2026-10-20T09:05:00+00:00"}
    frame = format_sse_event(ui.EVENT_INPUT_REQUEST, payload, event_id="x:3")
    assert frame.startswith("id: x:3\nevent: message.input.request\ndata: ")
    assert json.loads(frame.split("data: ", 1)[1]) == payload
