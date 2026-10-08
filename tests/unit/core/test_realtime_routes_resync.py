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


def test_permit_response_closes_body_iterator_shielded_on_cancelled_scope() -> None:
    import anyio

    done: list[bool] = []

    class _Body:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self) -> None:
            await asyncio.sleep(0)  # a cancelled scope would raise here without the shield
            done.append(True)

    async def run() -> None:
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        response = realtime_routes._PermitResponse(_Body(), asyncio.Event(), sem)  # type: ignore[arg-type]

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        with anyio.CancelScope() as scope:
            scope.cancel()
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert sem._value == 1  # permit released

    asyncio.run(run())
    assert done == [True]
