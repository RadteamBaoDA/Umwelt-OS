"""SSE wake-up via Redis pub/sub (`chat:response:<id>`) with a slow poll fallback; same ordering/dedup/resume."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from modules.chat import routes, signals
from modules.chat.models import StreamEvent
from tests.unit.modules.chat.test_p14_sse_stream import (
    RID,
    _DependencySession,
    _event,
    _install,
    _request,
    _Store,
)


class FakePubSub:
    """Minimal redis.asyncio PubSub: records subscribe/unsubscribe and serves queued messages."""

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.queue: list[dict[str, Any]] = []
        self.channels: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        if self.redis.fail_subscribe:
            raise ConnectionError("redis down")
        self.channels.append(channel)
        self.redis.subscribed.append(channel)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0) -> dict[str, Any] | None:
        assert ignore_subscribe_messages is True
        if not self.queue and timeout:
            self.redis.waits.append(timeout)
            self.redis.on_idle(self)  # the "other process" acts while the SSE handler is parked
        return self.queue.pop(0) if self.queue else None

    async def unsubscribe(self, channel: str) -> None:
        self.redis.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """Records waits (the fallback timeout the handler asks for) and lets tests act during an idle wait."""

    def __init__(self, on_idle: Callable[[FakePubSub], None] | None = None) -> None:
        self.on_idle = on_idle or (lambda _pubsub: None)
        self.waits: list[float] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.published: list[tuple[str, bytes]] = []
        self.fail_subscribe = False
        self.pubsubs: list[FakePubSub] = []

    def pubsub(self) -> FakePubSub:
        pubsub = FakePubSub(self)
        self.pubsubs.append(pubsub)
        return pubsub

    async def publish(self, channel: str, message: bytes) -> int:
        self.published.append((channel, message))
        return 1


def _signalled(store: _Store, redis: FakeRedis) -> Any:
    request = _request(store)
    request.app.state.redis = redis
    return request


async def _stream(store: _Store, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[float]]:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def spy(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", spy)
    response = await routes.get_response_events(
        RID, _signalled(store, redis), _DependencySession(store), last_event_id=None, cursor=None,  # type: ignore[arg-type]
    )
    chunks = [str(chunk) async for chunk in response.body_iterator]  # type: ignore[attr-defined]
    return chunks, sleeps


async def test_idle_stream_waits_on_the_signal_not_a_fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([], status="streaming")
    _install(monkeypatch, store)
    step = {"n": 0}

    def on_idle(pubsub: FakePubSub) -> None:
        step["n"] += 1
        if step["n"] == 1:
            store.events = [_event(1), _event(2)]  # the worker committed two events ...
        else:
            store.status = "completed"  # ... then finished
        pubsub.queue.append({"type": "message", "data": b"1"})  # ... and published after the commit

    redis = FakeRedis(on_idle)
    chunks, sleeps = await _stream(store, redis, monkeypatch)
    text = "".join(chunks)
    assert [line for line in text.splitlines() if line.startswith("id:")] == [f"id: {RID}:1", f"id: {RID}:2"]
    assert redis.subscribed == [f"chat:response:{RID}"]
    assert redis.waits == [signals.STREAM_SIGNAL_FALLBACK_INTERVAL] * 2  # slow fallback timeout, not 0.1-0.5 s
    assert sleeps == []  # no legacy backoff sleeps while subscribed
    assert redis.unsubscribed == [f"chat:response:{RID}"] and redis.pubsubs[0].closed


async def test_lost_signal_is_recovered_by_the_slow_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([], status="streaming")
    _install(monkeypatch, store)
    step = {"n": 0}

    def on_idle(_pubsub: FakePubSub) -> None:
        step["n"] += 1  # NO message is ever published: Redis dropped it
        if step["n"] == 1:
            store.events = [_event(1)]
        else:
            store.status = "completed"

    redis = FakeRedis(on_idle)
    chunks, _sleeps = await _stream(store, redis, monkeypatch)
    assert "".join(chunks).count("event: message.delta") == 1
    assert redis.waits == [signals.STREAM_SIGNAL_FALLBACK_INTERVAL] * 2


async def test_resume_cursor_and_dedup_are_unchanged_with_a_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([_event(n) for n in range(1, 6)], status="completed")
    _install(monkeypatch, store)
    redis = FakeRedis()
    request = _signalled(store, redis)
    response = await routes.get_response_events(
        RID, request, _DependencySession(store), last_event_id=f"{RID}:3", cursor=None,  # type: ignore[arg-type]
    )
    text = "".join([str(c) async for c in response.body_iterator])  # type: ignore[attr-defined]
    assert [line for line in text.splitlines() if line.startswith("id:")] == [f"id: {RID}:4", f"id: {RID}:5"]
    assert store.seq_floors[0] == 3  # the DB read still uses the seq cursor, not the signal payload


async def test_subscription_failure_falls_back_to_the_legacy_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([], status="streaming")
    _install(monkeypatch, store)
    redis = FakeRedis()
    redis.fail_subscribe = True
    seen = {"n": 0}
    real_sleep = asyncio.sleep

    async def spy(delay: float) -> None:
        seen["n"] += 1
        if seen["n"] == 3:
            store.status = "completed"
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", spy)
    response = await routes.get_response_events(
        RID, _signalled(store, redis), _DependencySession(store), last_event_id=None, cursor=None,  # type: ignore[arg-type]
    )
    [c async for c in response.body_iterator]  # type: ignore[attr-defined]
    assert seen["n"] == 3 and redis.waits == []


async def test_subscription_is_released_when_the_client_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store([], status="streaming")
    _install(monkeypatch, store)
    redis = FakeRedis()
    response = await routes.get_response_events(
        RID, _signalled(store, redis), _DependencySession(store), last_event_id=None, cursor=None,  # type: ignore[arg-type]
    )
    await anext(response.body_iterator)  # type: ignore[attr-defined,call-overload]
    await response.body_iterator.aclose()  # type: ignore[attr-defined]
    assert redis.unsubscribed == [f"chat:response:{RID}"]


async def test_publish_is_best_effort() -> None:
    class Broken:
        async def publish(self, *_a: Any) -> None:
            raise ConnectionError("down")

    await signals.publish_response_signal(Broken(), uuid4())  # must not raise
    redis = FakeRedis()
    run_id = uuid4()
    await signals.publish_response_signal(redis, run_id)
    assert redis.published == [(f"chat:response:{run_id}", b"1")]


def _pending_event(session: Session, response_id: UUID) -> StreamEvent:
    event = StreamEvent(response_id=response_id, seq=1, event_type="status", event_id=f"{response_id}:1", data={})
    session.add(event)  # attaches the instance to the session (no flush needed for object_session)
    return event


async def test_commit_hook_publishes_once_per_registered_run_after_commit() -> None:
    run_id, other = uuid4(), uuid4()
    redis = FakeRedis()
    session = Session()
    with signals.signal_run_commits(run_id, redis):
        for target in (run_id, run_id, other):  # two inserts for the run, one for an unregistered run
            signals._note_inserted(None, None, _pending_event(session, target))  # type: ignore[arg-type]
        assert session.info[signals._PENDING_KEY] == {run_id}
        signals._signal_after_commit(session)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert redis.published == [(f"chat:response:{run_id}", b"1")]
    assert signals._PENDING_KEY not in session.info


async def test_rollback_forgets_pending_signals() -> None:
    run_id = uuid4()
    redis = FakeRedis()
    session = Session()
    with signals.signal_run_commits(run_id, redis):
        signals._note_inserted(None, None, _pending_event(session, run_id))  # type: ignore[arg-type]
        signals._forget_after_rollback(session)
        signals._signal_after_commit(session)
        await asyncio.sleep(0)
    assert redis.published == []
