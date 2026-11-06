from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.worker_cursors import STATE_KEY, read_cursor, write_cursor

KEY = "k"


@pytest.mark.asyncio
async def test_generation_change_during_read_still_lets_next_write_land() -> None:
    newest = uuid4()
    state: dict[str, str] = {}

    async def bump(_key: str) -> bytes:
        state[f"_gen:{KEY}"] = "5"  # another job wrote while we awaited Redis
        state[KEY] = str(newest)
        return str(uuid4()).encode()

    redis = AsyncMock()
    redis.get = bump
    ctx: dict[str, object] = {STATE_KEY: state, "redis": redis}
    assert await read_cursor(ctx, KEY, {KEY}) == newest
    nxt = uuid4()
    await write_cursor(ctx, KEY, nxt, {KEY})
    assert state[KEY] == str(nxt) and state[f"_gen:{KEY}"] == "6"
