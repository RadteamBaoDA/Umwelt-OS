from __future__ import annotations

import asyncio

import pytest

from tests.integration.chat_eval.pacing import WINDOW_S, StartPacer, rerank_rpm


class FakeClock:
    """A clock and sleep pair: ``sleep`` advances ``now`` instead of waiting, and records each wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _pacer(rpm: int, clock: FakeClock) -> StartPacer:
    return StartPacer(rpm, clock=clock, sleep=clock.sleep)


def test_rerank_rpm_unset_blank_or_zero_is_unlimited() -> None:
    assert rerank_rpm({}) is None
    assert rerank_rpm({"CHAT_EVAL_RERANK_RPM": "  "}) is None
    assert rerank_rpm({"CHAT_EVAL_RERANK_RPM": "0"}) is None
    assert rerank_rpm({"CHAT_EVAL_RERANK_RPM": " 10 "}) == 10


@pytest.mark.parametrize("raw", ["-1", "ten", "1.5"])
def test_rerank_rpm_rejects_bad_values(raw: str) -> None:
    with pytest.raises(ValueError):
        rerank_rpm({"CHAT_EVAL_RERANK_RPM": raw})


def test_pacer_lets_rpm_starts_through_without_waiting() -> None:
    clock = FakeClock()
    pacer = _pacer(2, clock)
    asyncio.run(pacer.acquire())
    asyncio.run(pacer.acquire())
    assert clock.sleeps == []


def test_pacer_makes_the_next_start_in_the_window_wait() -> None:
    clock = FakeClock()
    pacer = _pacer(2, clock)

    async def run() -> None:
        await pacer.acquire()
        clock.now = 5.0
        await pacer.acquire()
        await pacer.acquire()  # third start inside the window: must wait for the first to age out

    asyncio.run(run())
    assert clock.sleeps == [WINDOW_S - 5.0]


def test_pacer_frees_a_slot_once_its_start_leaves_the_window() -> None:
    clock = FakeClock()
    pacer = _pacer(1, clock)

    async def run() -> None:
        await pacer.acquire()
        clock.now = WINDOW_S + 1.0
        await pacer.acquire()

    asyncio.run(run())
    assert clock.sleeps == []


def test_pacer_shares_the_window_across_concurrent_callers() -> None:
    clock = FakeClock()
    pacer = _pacer(2, clock)

    async def run() -> None:
        await asyncio.gather(*(pacer.acquire() for _ in range(3)))

    asyncio.run(run())
    assert clock.sleeps == [WINDOW_S]


def test_pacer_rejects_non_positive_rpm() -> None:
    with pytest.raises(ValueError):
        StartPacer(0)
