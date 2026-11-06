"""core.cron: argument validation, in-process run bookkeeping, Redis last-success mirror, overdue selection."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core import cron


@pytest.fixture(autouse=True)
def _clean_runs() -> None:
    cron.reset_cron_status()


def test_stable_offset_is_deterministic_bounded_and_salted() -> None:
    assert cron.stable_offset("job", 60) == cron.stable_offset("job", 60)
    assert 0 <= cron.stable_offset("job", 7) < 7
    # Same name, different salt yields an independent phase (not guaranteed distinct for one pair, so scan names).
    assert any(
        cron.stable_offset(f"j{i}", 1000) != cron.stable_offset(f"j{i}", 1000, "x")
        for i in range(20)
    )
    with pytest.raises(ValueError, match="modulus"):
        cron.stable_offset("job", 0)


@pytest.mark.parametrize("fn", [cron.second_set, cron.minute_set])
def test_period_sets_cover_the_minute_or_hour_at_a_fixed_stride(fn: Any) -> None:
    result = fn("job", 15)
    assert len(result) == 4
    assert {b - a for a, b in zip(sorted(result), sorted(result)[1:], strict=False)} == {15}
    for bad in (0, -5, 7):
        with pytest.raises(ValueError, match="divide 60"):
            fn("job", bad)


def test_second_of_minute_in_range() -> None:
    assert 0 <= cron.second_of_minute("job") < 60


@pytest.mark.parametrize(("period", "ttl"), [(1, 30_000), (60, 120_000), (10_000, 300_000)])
def test_lease_ttl_is_clamped(period: int, ttl: int) -> None:
    assert cron.lease_ttl_ms(period) == ttl


def test_record_run_tracks_success_failure_and_skips() -> None:
    cron.record_cron_run("a", 1.5, True, now=100.0)
    state = cron.record_cron_run("a", 2.0, False, now=200.0)
    assert (state.successes, state.failures) == (1, 1)
    assert (state.last_success_at, state.last_failure_at, state.last_duration_seconds) == (
        100.0,
        200.0,
        2.0,
    )
    cron.record_cron_skip("a")
    cron.record_cron_skip("new")
    status = cron.cron_status()
    assert status["a"].skipped == 1 and status["a"].successes == 1
    assert status["new"].skipped == 1 and status["new"].last_success_at is None
    # The returned mapping is a copy: mutating it must not change process state.
    status.clear()
    assert set(cron.cron_status()) == {"a", "new"}


class _Redis:
    def __init__(self, stored: dict[Any, Any] | None = None, fail: bool = False) -> None:
        self.stored = stored or {}
        self.fail = fail

    async def hset(self, key: str, name: str, value: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.stored[name] = value

    async def hgetall(self, key: str) -> dict[Any, Any]:
        return self.stored


async def test_persist_cron_run_stores_only_successes_and_swallows_errors() -> None:
    redis = _Redis()
    await cron.persist_cron_run(redis, "a", 1.2345, False, now=5.0)
    assert redis.stored == {}
    await cron.persist_cron_run(redis, "a", 1.2345, True, now=5.0)
    assert redis.stored == {"a": "5.000:1234"}
    await cron.persist_cron_run(_Redis(fail=True), "a", 1.0, True, now=5.0)  # must not raise


async def test_read_last_success_decodes_bytes_and_skips_malformed_entries() -> None:
    redis = _Redis({b"a": b"12.500:300", "b": "7.0:1", "bad": "oops:1", "empty": ""})
    assert await cron.read_last_success(redis) == {"a": 12.5, "b": 7.0}


def test_overdue_jobs_flags_missing_and_stale_sorted() -> None:
    periods = {"fresh": 60, "stale": 60, "never": 60, "edge": 60}
    last = {"fresh": 900.0, "stale": 100.0, "edge": 880.0}
    # edge is exactly CATCH_UP_FACTOR * period old (120s): not overdue; strictly older is.
    assert cron.overdue_jobs(periods, last, now=1000.0) == ["never", "stale"]


async def test_renew_loop_stops_when_lease_is_lost_or_redis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def instant_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    calls: list[str] = []

    async def lost(*_: Any) -> bool:
        calls.append("lost")
        return False

    monkeypatch.setattr(cron, "renew_lease", lost)
    await asyncio.wait_for(cron._renew_loop(object(), "job", "t", 30_000), 1)
    assert calls == ["lost"]

    async def boom(*_: Any) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(cron, "renew_lease", boom)
    await asyncio.wait_for(cron._renew_loop(object(), "job", "t", 30_000), 1)
