"""Regression: a stale-epoch cursor yields an epoch_changed resync carrying the head cursor."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from core import realtime_routes
from core.realtime import ReplayCursor


def test_stale_epoch_cursor_first_frame_is_epoch_changed_resync(monkeypatch) -> None:
    head = SimpleNamespace(epoch=uuid4(), sequence=7, floor_sequence=1)

    async def fake_prepare(_session, _request, _workspace_id, _workspace_header):
        # Admission/fence/session are covered by the scope tests; this exercises resync framing only.
        return SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), head

    monkeypatch.setattr(realtime_routes, "_prepare_realtime", fake_prepare)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(
            realtime_connections=asyncio.Semaphore(1),
        )),
    )
    stale = ReplayCursor(epoch=uuid4(), sequence=3).encode()

    async def run() -> str:
        response = await realtime_routes.stream_events(
            None, request, None, cursor=stale, last_event_id=None,
        )
        # Inspect the wrapped stream; the outer response only gates actual ASGI sends.
        return await anext(response.response.body_iterator)

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
            await asyncio.sleep(0.01)  # rollback await: CancelledError here if a task group cancelled us
            events.append("rollback-ok")

    async def run() -> int:
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        gone = asyncio.Event()

        async def receive():  # uvicorn httptools: blocks until disconnect, then returns it at once
            await gone.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        request = Request(scope, receive)

        async def body():
            while not await request.is_disconnected():
                async with _Session():
                    await asyncio.sleep(0.2)  # DB await in flight when the disconnect lands
                    yield ": ping"

        async def send(_message):
            return None

        asyncio.get_running_loop().call_later(0.05, gone.set)
        await realtime_routes._PermitResponse(body(), asyncio.Event(), sem)(scope, receive, send)
        return sem._value

    assert asyncio.run(run()) == 1
    assert events == ["rollback-ok"]


def test_polling_response_lets_inflight_db_call_finish_and_returns_connection_on_disconnect() -> None:
    """Disconnect mid-poll must not cancel the body: the in-flight 'query' completes and nothing stays checked out.

    Starlette's default task group would cancel the body mid-await (leaking the connection via a cancelled
    SQLAlchemy terminate()); the polling response runs the body uncancelled and exits at the next
    is_disconnected() check. `checked_out` stands in for `pool.checkedout()`.
    """
    import warnings

    from starlette.requests import Request

    from core import realtime_routes

    async def run() -> tuple[int, bool, int]:
        gone = asyncio.Event()
        state = {"checked_out": 0, "finished_query": False, "sends": 0}

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        request = Request(scope, receive)

        class _Conn:
            async def __aenter__(self):
                state["checked_out"] += 1

            async def __aexit__(self, *exc):
                state["checked_out"] -= 1

        async def body():
            while not await request.is_disconnected():
                async with _Conn():
                    await asyncio.sleep(0.2)  # query in flight when the disconnect lands
                    state["finished_query"] = True
                yield ": ping"

        async def send(_message):
            state["sends"] += 1

        asyncio.get_running_loop().call_later(0.05, gone.set)
        response = realtime_routes._PollingStreamingResponse(body(), media_type="text/event-stream")
        await response(scope, receive, send)
        return state["checked_out"], state["finished_query"], state["sends"]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        checked_out, finished, _ = asyncio.run(run())
    assert checked_out == 0 and finished
