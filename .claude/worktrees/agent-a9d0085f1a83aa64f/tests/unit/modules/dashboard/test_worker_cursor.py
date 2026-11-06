"""N-F12: dashboard worker cursors live in the startup-installed shared state."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from modules.dashboard import worker


@pytest.mark.asyncio
async def test_cursor_survives_two_ctx_copies_with_redis_down() -> None:
    """ARQ copies ctx per job; the nested state object keeps fallback progress and beats a stale GET."""
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = RuntimeError("down")
    base = {"redis": redis, "w2_cursor_state": {}}
    cursor = uuid4()
    await worker._write_workspace_cursor({**base}, worker.BRIEF_CURSOR_KEY, cursor)
    redis.get.return_value = str(uuid4()).encode()
    assert await worker._read_workspace_cursor({**base}, worker.BRIEF_CURSOR_KEY) == cursor


@pytest.mark.asyncio
async def test_synced_redis_value_wins_and_none_clears() -> None:
    """With a healthy Redis the remote value is used, and a None write clears the local cursor."""
    redis = AsyncMock()
    remote = uuid4()
    redis.get.return_value = str(remote)
    ctx = {"redis": redis, "w2_cursor_state": {}}
    assert await worker._read_workspace_cursor(ctx, worker.HIGHLIGHT_CURSOR_KEY) == remote
    await worker._write_workspace_cursor(ctx, worker.HIGHLIGHT_CURSOR_KEY, None)
    redis.get.return_value = None
    assert await worker._read_workspace_cursor(ctx, worker.HIGHLIGHT_CURSOR_KEY) is None
    with pytest.raises(KeyError):
        await worker._read_workspace_cursor(ctx, "unknown")
