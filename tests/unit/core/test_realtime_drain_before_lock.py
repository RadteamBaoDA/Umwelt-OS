"""Realtime guarded send drains the transport before taking the DB lock."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core import realtime_routes as rr


def _request(factory: Any) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(
        session_factory=factory, settings=SimpleNamespace(multi_workspace_enabled=False)), exception_handlers={})
    return Request({"type": "http", "app": app, "headers": []})


class _Session:
    pass


def _wrap() -> Any:
    return rr._RealtimeResponse(
        Response(b"hello"), request=_request(_Session), workspace=1, access_fence=2, auth_session=3)  # type: ignore[arg-type]


def _patch(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    async def lock(session, **kw):
        order.append("lock")

    async def cleanup(session):
        return None

    monkeypatch.setattr(rr.workspaces, "lock_access_fence", lock)
    monkeypatch.setattr(rr, "_cleanup_session", cleanup)


def test_body_drain_precedes_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    _patch(monkeypatch, order)

    async def send(m):
        order.append(f"{m['type']}:{m.get('body', b'')!r}")

    asyncio.run(_wrap()({"type": "http", "headers": []}, None, send))
    assert order == ["lock", "http.response.start:b''", "http.response.body:b''", "lock",
                     "http.response.body:b'hello'"]


def test_drain_timeout_never_takes_body_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    _patch(monkeypatch, order)
    monkeypatch.setattr(rr, "SEND_TIMEOUT_SECONDS", 0.05)

    async def send(m):
        if m["type"] == "http.response.body":
            await asyncio.sleep(10)

    # Uncertain send after start: the denial is absorbed and no terminal body is attempted.
    asyncio.run(_wrap()({"type": "http", "headers": []}, None, send))
    assert order == ["lock"]  # start lock only; the stalled drain timed out before the body lock
