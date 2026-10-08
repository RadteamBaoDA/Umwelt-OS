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
