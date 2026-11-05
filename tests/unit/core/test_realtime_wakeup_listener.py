import asyncio
from uuid import uuid4

import pytest

from core.realtime import routes
from core.realtime.routes import run_realtime_wakeup_listener


class _PubSub:
    """Scripted pub/sub: yields queued messages, then raises or blocks."""

    def __init__(self, script: list[object]) -> None:
        self.script = script
        self.patterns: list[str] = []
        self.closed = False

    async def psubscribe(self, pattern: str) -> None:
        self.patterns.append(pattern)

    async def get_message(self, **_kwargs: object) -> object:
        if not self.script:
            await asyncio.sleep(10)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self, pubsubs: list[_PubSub]) -> None:
        self.pubsubs = pubsubs

    def pubsub(self) -> _PubSub:
        return self.pubsubs.pop(0)


@pytest.mark.asyncio
async def test_listener_wakes_workspace_stream_and_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker publish wakes the matching workspace; a Redis error reconnects instead of ending the task."""
    monkeypatch.setattr(routes, "LISTENER_RETRY_SECONDS", (0,))
    workspace_id = uuid4()
    woken: list[object] = []
    monkeypatch.setattr(routes, "notify_realtime_wakeup", woken.append)
    first = _PubSub([
        {"channel": b"realtime:workspace:not-a-uuid"},
        {"channel": f"realtime:workspace:{workspace_id}".encode()},
        ConnectionError("down"),
    ])
    second = _PubSub([{"channel": f"realtime:workspace:{workspace_id}"}])
    task = asyncio.create_task(run_realtime_wakeup_listener(_Redis([first, second])))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert woken == [workspace_id, workspace_id]
    assert first.patterns == second.patterns == ["realtime:workspace:*"]
    assert first.closed and second.closed
