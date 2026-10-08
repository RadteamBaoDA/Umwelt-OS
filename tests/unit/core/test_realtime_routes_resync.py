"""Regression: a stale-epoch cursor yields an epoch_changed resync carrying the head cursor."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from core import realtime_routes
from core.realtime import ReplayCursor


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_stale_epoch_cursor_first_frame_is_epoch_changed_resync(monkeypatch) -> None:
    head = SimpleNamespace(epoch=uuid4(), sequence=7, floor_sequence=1)

    async def fake_head(_session):
        return head

    async def fake_current(_request):
        return True

    monkeypatch.setattr(realtime_routes, "current_head", fake_head)
    monkeypatch.setattr(realtime_routes, "_session_is_current", fake_current)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            session_factory=lambda: _FakeSession(),
            realtime_connections=asyncio.Semaphore(1),
        )),
    )
    stale = ReplayCursor(epoch=uuid4(), sequence=3).encode()

    async def run() -> str:
        response = await realtime_routes.stream_events(request, cursor=stale, last_event_id=None)
        return await anext(response.body_iterator)

    frame = asyncio.run(run())
    assert frame.startswith("event: resync_required")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {
        "reason": "epoch_changed",
        "snapshot_cursor": ReplayCursor(epoch=head.epoch, sequence=7).encode(),
    }


def test_permit_response_disconnect_spec_2_3_does_not_cancel_db_rollback() -> None:
    from typing import Self

    from starlette.requests import Request

    events: list[str] = []

    class _Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            await asyncio.sleep(0.01)  # rollback await: raises CancelledError if the task group cancelled us
            events.append("rollback-ok")

    async def run() -> int:
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        deadline = asyncio.get_running_loop().time() + 0.05

        async def receive():
            # is_disconnected() polls with an already-cancelled scope: only a ready message gets through
            if asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        request = Request(scope, receive)

        async def body():
            async with _Session():
                while not await request.is_disconnected():
                    await asyncio.sleep(0.02)  # DB await inside the transaction
                    yield ": ping"

        async def send(_message):
            return None

        response = realtime_routes._PermitResponse(body(), asyncio.Event(), sem)
        await response(scope, receive, send)
        return sem._value

    assert asyncio.run(run()) == 1
    assert events == ["rollback-ok"]
