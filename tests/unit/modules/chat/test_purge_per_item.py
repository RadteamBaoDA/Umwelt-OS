"""Chat expiry purge: each candidate is its own transaction; the privacy lock never spans items."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.chat import worker


@pytest.mark.asyncio
async def test_purge_commits_between_items_and_locks_once_per_transaction() -> None:
    """Two expired conversations (one fails the locked expiry re-check) -> commit before each item."""
    now = datetime.now(UTC)
    expired = SimpleNamespace(
        ephemeral=True, expires_at=now - timedelta(hours=1), actor_user_id=uuid4(),
    )
    renewed = SimpleNamespace(
        ephemeral=True, expires_at=now + timedelta(hours=1), actor_user_id=uuid4(),
    )
    locked = [renewed, expired]  # served in order to the FOR UPDATE parent lookups
    events: list[str] = []

    async def scalar(stmt: object) -> object:
        """Serve locked parents for FOR UPDATE selects and zero for count selects."""
        return locked.pop(0) if "FOR UPDATE" in str(stmt) else 0

    session = MagicMock()
    session.scalar = scalar
    session.scalars = AsyncMock(side_effect=[
        SimpleNamespace(all=lambda: [uuid4(), uuid4()]),
    ])
    session.execute = AsyncMock(return_value=SimpleNamespace(all=list))
    session.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    async def strict_lock(_s: object) -> None:
        """Fail if the privacy lock is requested twice without an intervening commit."""
        assert not events or events[-1] == "commit"
        events.append("lock")

    deleted = AsyncMock(return_value=True)
    with (
        patch("modules.chat.public.delete_conversation", deleted),
        patch("modules.memory.public.lock_export_privacy", strict_lock),
        patch.object(worker, "purge_terminal_message_deltas", AsyncMock(return_value=0)),
    ):
        await worker.purge_expired_chat_runs({"session_factory": lambda: session})

    assert events.count("lock") == 2
    assert events.count("commit") >= 2
    deleted.assert_awaited_once()
