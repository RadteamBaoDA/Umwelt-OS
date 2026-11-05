"""Dispatcher wake debounce: coalescing inside WAKE_FLAG_TTL_MS, re-enqueue after it, cron jobs never dead-lettered.

Covered continuation paths (all funnel through ``ingestion_worker._wake_dispatcher_after_commit`` ->
``core.cron.notify_on_commit`` -> wake hook -> ``core.cron.wake``): ``_settle_stage``, ``_schedule_transient_retry``,
``_settle_failure`` and ``_defer_until_observations_enabled`` (the four return-to-pending paths asserted structurally
in ``test_ingestion_dispatch_wake``). Page-full continuation, stale-scan recovery and catch-up are not separate wake
call sites: they run inside ``dispatch_pending_work`` itself and re-enter via the same debounced ``wake``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.worker import main as worker_main
from core import cron, worker_dlq
from modules.ingestion import worker as ingestion_worker

JOB = "dispatch_pending_work"


class TtlRedis:
    """Redis double with a manual clock: SET NX PX honours expiry, enqueue_job records calls."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.expiry: dict[str, int] = {}
        self.enqueued: list[dict[str, Any]] = []

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool:
        """SET with NX; an expired key counts as absent."""
        if nx and self.expiry.get(key, -1) > self.now_ms:
            return False
        self.expiry[key] = self.now_ms + (px or 10**9)
        return True

    async def delete(self, *keys: str) -> int:
        """DEL."""
        return sum(self.expiry.pop(k, None) is not None for k in keys)

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> object:
        """Record the enqueue."""
        self.enqueued.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_wake_enqueues_once_coalesces_inside_ttl_and_enqueues_again_after() -> None:
    """First wake enqueues, wakes inside the TTL coalesce, a wake at/after the TTL enqueues again."""
    redis = TtlRedis()
    assert await cron.wake(redis, JOB)
    redis.now_ms = cron.WAKE_FLAG_TTL_MS - 1
    assert not await cron.wake(redis, JOB)
    assert not await cron.wake(redis, JOB)
    assert len(redis.enqueued) == 1
    redis.now_ms = cron.WAKE_FLAG_TTL_MS
    assert await cron.wake(redis, JOB)
    assert len(redis.enqueued) == 2
    assert redis.enqueued[0]["_job_id"] != redis.enqueued[1]["_job_id"]


@pytest.mark.asyncio
async def test_continuation_commits_coalesce_through_the_real_commit_hook() -> None:
    """Several sessions committing a continuation wake inside the TTL enqueue one run; one after the TTL enqueues again."""
    redis = TtlRedis()

    async def hook(name: str, defer_seconds: float = 1.0) -> None:
        await cron.wake(redis, name, defer_seconds=defer_seconds)

    async def commit_continuation(delay: float) -> None:
        session: Any = Session(create_engine("sqlite://"))
        ingestion_worker._wake_dispatcher_after_commit(session, delay)
        session.commit()  # after_commit schedules the hook task on the running loop
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session.close()

    cron.set_wake_hook(hook)
    try:
        for delay in (1.0, 60.0, 0.1):
            await commit_continuation(delay)
        assert len(redis.enqueued) == 1
        assert redis.enqueued[0]["_defer_by"] == 1.0  # first wake's delay; later ones were coalesced
        redis.now_ms = cron.WAKE_FLAG_TTL_MS + 1
        await commit_continuation(60.0)
        assert len(redis.enqueued) == 2
        assert redis.enqueued[1]["_defer_by"] == 5.0  # delay capped at the debounce window
    finally:
        cron.set_wake_hook(None)


@pytest.mark.asyncio
async def test_started_wake_job_clears_flag_so_next_commit_enqueues_inside_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cron_job drops the flag on start, so a commit during the run queues the next wake before TTL expiry."""
    redis = TtlRedis()

    async def noop(_ctx: dict[str, Any]) -> None: ...

    monkeypatch.setitem(worker_main._CRON_GUARDED, JOB, noop)  # no real dispatcher run
    assert await cron.wake(redis, JOB)
    assert not await cron.wake(redis, JOB)
    await worker_main.run_cron_job({"redis": redis}, JOB, True)
    assert await cron.wake(redis, JOB)
    assert len(redis.enqueued) == 2


@pytest.mark.asyncio
async def test_cron_jobs_never_go_to_the_dead_letter_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing cron job is not recorded in worker_dead_letters (a poll's next tick is its retry);
    the same failure through a dead_letter-wrapped event job is, proving the spy would have fired."""
    recorded: list[str] = []

    async def spy(*_a: Any, **kwargs: Any) -> None:
        recorded.append(str(kwargs["job_name"]))

    monkeypatch.setattr(worker_dlq, "record_dead_letter", spy)

    async def boom(ctx: dict[str, Any]) -> None:
        raise RuntimeError("poll failed")

    monkeypatch.setitem(worker_main._CRON_GUARDED, "boom_poll", boom)
    ctx: dict[str, Any] = {"session_factory": object(), "job_try": 1}
    [func] = worker_main.CronWorkerSettings.functions
    assert func.name == "run_cron_job"
    with pytest.raises(RuntimeError):
        await func.coroutine(ctx, "boom_poll", False)
    assert recorded == []

    with pytest.raises(RuntimeError):
        await worker_dlq.dead_letter(boom, max_tries=1, queue="q")(ctx)
    assert recorded == ["boom"]
