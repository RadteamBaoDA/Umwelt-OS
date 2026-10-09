"""Publication gate: bounded re-fenced sends, rollback on every exit, ungated pass-through."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core import publication
from core.publication import PublicationGateMiddleware
from core.workspaces.schemas import PublicationFence


class _Session:
    def __init__(self) -> None:
        self.rolled_back = 0
        self.closed = 0

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def close(self) -> None:
        self.closed += 1


def _scope(armed: bool) -> tuple[dict[str, Any], list[_Session]]:
    sessions: list[_Session] = []

    def factory() -> _Session:
        sessions.append(_Session())
        return sessions[-1]

    app = SimpleNamespace(state=SimpleNamespace(
        session_factory=factory, settings=SimpleNamespace(multi_workspace_enabled=False)))
    fence = SimpleNamespace(scope=1, access_fence=2, auth_session=3, grants=())
    return {"type": "http", "app": app, "state": {"publication_fence": fence} if armed else {}}, sessions


def _downstream(*bodies: bytes):
    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i, body in enumerate(bodies):
            await send({"type": "http.response.body", "body": body, "more_body": i < len(bodies) - 1})
    return app


async def _receive() -> dict:
    return {"type": "http.disconnect"}


class _Locks:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ok_fences = 99
        self.status = 404


@pytest.fixture
def locks(monkeypatch):
    state = _Locks()

    async def fence(session, **kw):
        state.calls.append("fence")
        if state.calls.count("fence") > state.ok_fences:
            raise HTTPException(status_code=state.status, detail="x")

    async def grants(session, **kw):
        state.calls.append("grants")

    monkeypatch.setattr(publication.workspaces, "lock_access_fence", fence)
    monkeypatch.setattr(publication.workspaces, "lock_resource_grants", grants)
    return state


def _run(app, scope, sent):
    async def send(m):
        sent.append(m)

    asyncio.run(PublicationGateMiddleware(app)(scope, _receive, send))


def test_ungated_response_passes_through(locks) -> None:
    scope, sessions = _scope(False)
    sent: list[dict] = []
    _run(_downstream(b"a"), scope, sent)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert not sessions and not locks.calls


def test_gated_sends_lock_and_roll_back(locks) -> None:
    scope, sessions = _scope(True)
    sent: list[dict] = []
    _run(_downstream(b"a", b""), scope, sent)
    assert len(sent) == 3
    assert locks.calls == ["fence", "grants"] * 2  # start + one non-empty body; empty body ungated
    assert len(sessions) == 2 and all(s.rolled_back == 1 and s.closed == 1 for s in sessions)


@pytest.mark.parametrize(("status", "expected"), [(404, 404), (409, 409), (401, 404)])
def test_revoke_before_start_sends_404_or_409(locks, status, expected) -> None:
    locks.ok_fences, locks.status = 0, status
    scope, sessions = _scope(True)
    sent: list[dict] = []
    _run(_downstream(b"a"), scope, sent)
    assert sent[0]["status"] == expected
    assert sessions[0].rolled_back == 1


def test_revoke_between_start_and_body_aborts(locks) -> None:
    locks.ok_fences = 1
    scope, sessions = _scope(True)
    sent: list[dict] = []
    with pytest.raises(RuntimeError, match="publication_revoked"):
        _run(_downstream(b"a"), scope, sent)
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert all(s.rolled_back == 1 for s in sessions)


def test_blocking_send_times_out_and_rolls_back(locks, monkeypatch) -> None:
    monkeypatch.setattr(publication, "SEND_TIMEOUT_SECONDS", 0.05)
    scope, sessions = _scope(True)

    async def send(m):
        await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        asyncio.run(PublicationGateMiddleware(_downstream(b"a"))(scope, _receive, send))
    assert sessions[0].rolled_back == 1 and sessions[0].closed == 1


def test_require_publication_gate_sets_state() -> None:
    fence = PublicationFence(scope=None, access_fence=None, auth_session=None, grants=())  # type: ignore[arg-type]
    request = Request({"type": "http", "headers": [], "state": {}})
    publication.require_publication_gate(request, fence)
    assert request.state.publication_fence is fence
    with pytest.raises(TypeError):
        publication.require_publication_gate(request, object())  # type: ignore[arg-type]
