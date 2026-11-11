"""The 409 for a second turn / edit while a run is active (C3 retry relies on it)."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.chat._routes.guards import _reject_active_response


@pytest.mark.asyncio
async def test_active_run_rejects_with_409_and_idle_conversation_passes() -> None:
    """Any pending/streaming/awaiting-input run in the conversation blocks edit and send with 409."""
    busy = AsyncMock()
    busy.scalar = AsyncMock(return_value=uuid4())
    with pytest.raises(HTTPException) as exc:
        await _reject_active_response(busy, uuid4())
    assert exc.value.status_code == 409

    idle = AsyncMock()
    idle.scalar = AsyncMock(return_value=None)
    await _reject_active_response(idle, uuid4())
