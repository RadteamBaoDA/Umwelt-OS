"""Cron scheduling: hashed offsets, period preservation, lease single-flight, catch-up, wake."""
from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

from apps.worker import main as worker_main
from apps.worker.cron_schedule import CronSpec, fire_times, second_load
from core import cron


class FakeRedis:
    """Minimal Redis double: SET NX PX, compare-and-delete/pexpire via eval, hash, enqueue."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hash: dict[str, str] = {}
        self.enqueued: list[tuple[Any, ...]] = []

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def eval(self, script: str, _n: int, key: str, token: str, *rest: Any) -> int:
        if self.kv.get(key) != token:
            return 0
        if "'del'" in script:
            del self.kv[key]
        return 1

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self.kv.pop(key, None) is not None)

    async def hset(self, name: str, key: str, value: str) -> None:
        self.hash[key] = value

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hash)

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> object | None:
        self.enqueued.append((args, kwargs))
        return object()


def test_offsets_are_stable_and_spread() -> None:
    assert cron.stable_offset("job", 60) == cron.stable_offset("job", 60)
    assert len({cron.stable_offset(f"job{i}", 60) for i in range(30)}) > 15
    assert cron.second_set("x", 5) <= set(range(60)) and len(cron.second_set("x", 5)) == 12
    assert len(cron.minute_set("x", 15)) == 4
    with pytest.raises(ValueError):
        cron.second_set("x", 7)


def test_every_job_keeps_its_period_and_load_is_spread() -> None:
    for spec in (*worker_main.CRON_SPECS, *worker_main.CHAT_CRON_SPECS):
        times = fire_times(spec)
        if spec.period_seconds < 60:
            seconds = sorted(times["second"])
            assert {b - a for a, b in itertools.pairwise(seconds)} == {spec.period_seconds}
        elif spec.period_seconds < 3600:
            assert times["minute"] is None or len(times["minute"]) == 3600 // spec.period_seconds
    load = second_load(worker_main.CRON_SPECS)
    assert max(load.values()) <= 5, load  # was 8 jobs at second :00 plus 24 at hh:00:00


def test_hourly_jobs_no_longer_share_minute_zero() -> None:
    hourly = [fire_times(s)["minute"] for s in worker_main.CRON_SPECS if s.period_seconds == 3600]
    assert len(set(hourly)) > 1 and hourly.count(0) <= 1


def test_cron_worker_registers_every_spec_by_arq_name() -> None:
    jobs = worker_main.CronWorkerSettings.cron_jobs
    assert [j.name for j in jobs] == [f"cron:{s.name}" for s in worker_main.CRON_SPECS]  # type: ignore[attr-defined]
    # run_cron_job is registered behind traced_job (strips the API's trace carrier) under its own name.
    assert [f.coroutine.__wrapped__ for f in worker_main.CronWorkerSettings.functions] == [worker_main.run_cron_job]
    assert [f.name for f in worker_main.CronWorkerSettings.functions] == ["run_cron_job"]
    assert worker_main.CronWorkerSettings.keep_result == 0
    assert worker_main.CronWorkerSettings.queue_name == cron.CRON_QUEUE
    assert not hasattr(worker_main.WorkerSettings, "cron_jobs")  # ingestion slots never run polls


def test_dispatch_poller_is_a_slow_safety_net() -> None:
    periods = {s.name: s.period_seconds for s in worker_main.CRON_SPECS}
    assert periods["dispatch_pending_work"] >= 30 and periods["index_pending_chunks"] >= 60


@pytest.mark.asyncio
async def test_lease_is_exclusive_and_token_guarded() -> None:
    redis = FakeRedis()
    first = await cron.acquire_lease(redis, "j", 1000)
    assert first and await cron.acquire_lease(redis, "j", 1000) is None
    assert not await cron.release_lease(redis, "j", "other")
    assert await cron.renew_lease(redis, "j", first, 1000)
    assert await cron.release_lease(redis, "j", first)
    assert await cron.acquire_lease(redis, "j", 1000)


@pytest.mark.asyncio
async def test_single_flight_skips_overlap_and_records() -> None:
    cron.reset_cron_status()
    redis = FakeRedis()
    gate = asyncio.Event()
    started = asyncio.Event()
    runs = 0

    async def slow(ctx: dict[str, Any]) -> str:
        nonlocal runs
        runs += 1
        started.set()
        await gate.wait()
        return "done"

    guarded = cron.single_flight(slow, name="slow", period_seconds=5)
    ctx = {"redis": redis}
    first = asyncio.create_task(guarded(ctx))
    await started.wait()
    assert await guarded(ctx) is None  # another replica / overlapping tick
    gate.set()
    assert await first == "done" and runs == 1
    assert "cron:lease:slow" not in redis.kv  # released in finally
    status = cron.cron_status()["slow"]
    assert status.successes == 1 and status.skipped == 1 and status.last_success_at is not None
    assert "slow" in redis.hash


@pytest.mark.asyncio
async def test_single_flight_releases_and_records_failure() -> None:
    cron.reset_cron_status()
    redis = FakeRedis()

    async def boom(ctx: dict[str, Any]) -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await cron.single_flight(boom, name="boom", period_seconds=60)({"redis": redis})
    assert not redis.kv and cron.cron_status()["boom"].failures == 1 and "boom" not in redis.hash


@pytest.mark.asyncio
async def test_catch_up_runs_only_jobs_older_than_twice_period() -> None:
    now = 10_000.0
    periods = {"fresh": 60, "stale": 60, "never": 3600}
    assert cron.overdue_jobs(periods, {"fresh": now - 100, "stale": now - 121}, now=now) == ["never", "stale"]
    redis = FakeRedis()
    redis.hash["purge_expired_sessions"] = "1.0:5"  # ancient success
    queued = await worker_main.catch_up_missed_runs({"redis": redis})
    assert "purge_expired_sessions" in queued and "recover_graph_work" not in queued  # startup jobs excluded
    args, kwargs = next(item for item in redis.enqueued if item[0][1] == "purge_expired_sessions")
    assert args == (cron.RUN_CRON_FUNCTION, "purge_expired_sessions", False) and kwargs["_queue_name"] == cron.CRON_QUEUE and 0 <= kwargs["_defer_by"] < 30


class ArqLikeRedis(FakeRedis):
    """FakeRedis that models arq's enqueue rules: an id is refused while queued or while a result is kept."""

    def __init__(self) -> None:
        super().__init__()
        self.queued: dict[str, tuple[Any, ...]] = {}
        self.results: set[str] = set()

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> object | None:
        job_id = kwargs["_job_id"]
        if job_id in self.queued or job_id in self.results:
            return None
        self.queued[job_id] = args
        self.enqueued.append((args, kwargs))
        return object()

    def finish(self, job_id: str, *, keep_result: bool) -> None:
        self.queued.pop(job_id)
        if keep_result:
            self.results.add(job_id)


@pytest.mark.asyncio
async def test_wake_uses_unique_ids_and_is_debounced_by_flag() -> None:
    redis = ArqLikeRedis()
    assert await cron.wake(redis, "dispatch_pending_work")
    args, kwargs = redis.enqueued[0]
    assert args == (cron.RUN_CRON_FUNCTION, "dispatch_pending_work", True)
    assert kwargs["_job_id"].startswith("wake:dispatch_pending_work:") and kwargs["_queue_name"] == cron.CRON_QUEUE
    assert not await cron.wake(redis, "dispatch_pending_work")  # burst coalesced while the wake is pending

    class Broken(FakeRedis):
        async def enqueue_job(self, *a: Any, **k: Any) -> object | None:
            raise OSError("down")

    broken = Broken()
    assert not await cron.wake(broken, "x") and "cron:wake:x" not in broken.kv  # flag released on failure


@pytest.mark.asyncio
async def test_wake_soon_after_finished_or_failed_wake_still_runs() -> None:
    redis = ArqLikeRedis()
    for _ in range(3):  # each previous wake finished AND kept a result (failed run under keep_result > 0)
        assert await cron.wake(redis, "dispatch_pending_work")
        job_id = next(iter(redis.queued))
        await cron.clear_wake_flag(redis, "dispatch_pending_work")  # what run_cron_job does on start
        redis.finish(job_id, keep_result=True)
    assert len(redis.enqueued) == 3 and len({k["_job_id"] for _, k in redis.enqueued}) == 3


@pytest.mark.asyncio
async def test_wake_during_held_lease_triggers_one_rerun() -> None:
    cron.reset_cron_status()
    redis = FakeRedis()
    gate = asyncio.Event()
    started = asyncio.Event()
    runs = 0

    async def poller(ctx: dict[str, Any]) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            started.set()
            await gate.wait()

    guarded = cron.single_flight(poller, name="poller", period_seconds=30)
    holder = asyncio.create_task(guarded({"redis": redis}))
    await started.wait()
    assert await guarded({"redis": redis, "cron_wake": True}) is None  # skipped but remembered
    assert "cron:rerun:poller" in redis.kv
    gate.set()
    await holder
    assert runs == 2 and "cron:rerun:poller" not in redis.kv and "cron:lease:poller" not in redis.kv


@pytest.mark.asyncio
async def test_run_cron_job_clears_flag_and_ignores_unknown_names() -> None:
    redis = FakeRedis()
    redis.kv["cron:wake:nope"] = "1"
    assert await worker_main.run_cron_job({"redis": redis}, "nope", True) is None
    assert "cron:wake:nope" in redis.kv  # unknown job: untouched
    name = worker_main.CRON_SPECS[0].name
    redis.kv[f"cron:wake:{name}"] = "1"
    worker_main._CRON_GUARDED[name] = _noop_guard  # type: ignore[assignment]
    try:
        await worker_main.run_cron_job({"redis": redis}, name, True)
    finally:
        worker_main._CRON_GUARDED[name] = _ORIGINAL[name]
    assert f"cron:wake:{name}" not in redis.kv


async def _noop_guard(ctx: dict[str, Any]) -> None:
    return None


_ORIGINAL = dict(worker_main._CRON_GUARDED)


def test_notify_on_commit_registers_one_listener_pair_and_survives_rollback() -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    woken: list[str] = []

    async def hook(name: str) -> None:
        woken.append(name)

    session = Session(create_engine("sqlite://"))
    # Class-level Session listeners (e.g. modules.chat.signals) also appear in dispatch; count only ours.
    commit_base, rollback_base = len(session.dispatch.after_commit), len(session.dispatch.after_soft_rollback)
    cron.set_wake_hook(hook)
    try:
        cron.notify_on_commit(session, "a")
        cron.notify_on_commit(session, "b")
        assert len(session.dispatch.after_commit) == commit_base + 1
        assert len(session.dispatch.after_soft_rollback) == rollback_base + 1
        session.execute(text("select 1"))  # open a transaction so rollback is observable
        session.rollback()
        assert session.info["cron_wakes"] == {}  # rollback discarded, listeners remain single
        cron.notify_on_commit(session, "a")
        assert len(session.dispatch.after_commit) == commit_base + 1
    finally:
        cron.set_wake_hook(None)
        session.close()


def test_cron_spec_rejects_unsupported_period() -> None:
    async def noop(ctx: dict[str, Any]) -> None: ...

    with pytest.raises(ValueError):
        fire_times(CronSpec(noop, 86400))  # daily needs a pinned hour


def test_notify_on_commit_keeps_smallest_defer_and_passes_it_to_the_hook() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    session = Session(create_engine("sqlite://"))
    cron.set_wake_hook(None)
    try:
        # no hook installed: nothing is recorded
        cron.notify_on_commit(session, "a", defer_seconds=5.0)
        assert "cron_wakes" not in session.info

        async def hook(name: str, defer_seconds: float = 1.0) -> None: ...

        cron.set_wake_hook(hook)
        cron.notify_on_commit(session, "a", defer_seconds=5.0)
        cron.notify_on_commit(session, "a", defer_seconds=2.0)
        cron.notify_on_commit(session, "a", defer_seconds=4.0)
        assert session.info["cron_wakes"] == {"a": 2.0}
    finally:
        cron.set_wake_hook(None)
        session.close()


def test_cron_jobs_and_wake_function_allow_retry_on_heavy_slot_contention() -> None:
    from apps.worker.cron_schedule import CRON_MAX_TRIES

    assert CRON_MAX_TRIES > 1
    assert worker_main.CronWorkerSettings.max_tries == CRON_MAX_TRIES
    assert worker_main.CronWorkerSettings.functions[0].max_tries == CRON_MAX_TRIES
    assert all(job.max_tries == CRON_MAX_TRIES for job in worker_main.CronWorkerSettings.cron_jobs)  # type: ignore[attr-defined]
