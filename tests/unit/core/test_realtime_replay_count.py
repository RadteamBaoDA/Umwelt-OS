"""`_bounded_replay_count` derives totals from head windows and fails closed on corruption."""

from __future__ import annotations

import pytest

from core import realtime


class _Result:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row

    async def execute(self, _stmt):
        return _Result(self._row)


@pytest.mark.asyncio
async def test_sums_windows():
    assert await realtime._bounded_replay_count(_Session((7, 1))) == 7  # type: ignore[arg-type]
    assert await realtime._bounded_replay_count(_Session((0, None))) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_negative_window_raises():
    with pytest.raises(realtime._ReplayStorageError):
        await realtime._bounded_replay_count(_Session((5, -1)))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_over_cap_raises():
    with pytest.raises(realtime._ReplayStorageError):
        await realtime._bounded_replay_count(
            _Session((realtime.MAX_INSTANCE_REPLAY_EVENTS + 1, 1)))  # type: ignore[arg-type]
