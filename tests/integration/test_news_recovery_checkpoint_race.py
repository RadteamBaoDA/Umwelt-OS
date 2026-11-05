"""Two concurrent transactions ensuring the same workspace recovery checkpoint both succeed (one row)."""

import asyncio
import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modules.news.models import NewsRecoveryCheckpoint
from modules.news.worker import _ensure_recovery_checkpoint

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)


async def test_concurrent_checkpoint_creation_yields_one_row(
    ready_owner_client: object, committed_engine: AsyncEngine,
) -> None:
    """Worker and cron racing on first creation must not raise UniqueViolation."""
    factory = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with factory() as session:
        workspace_id = await session.scalar(text("SELECT id FROM workspaces LIMIT 1"))
        await session.execute(text("DELETE FROM news_recovery_checkpoints WHERE workspace_id = :w"),
                              {"w": workspace_id})
        await session.commit()

    async def attempt() -> None:
        async with factory() as session:
            await _ensure_recovery_checkpoint(session, workspace_id)
            await asyncio.sleep(0.2)  # hold the transaction open so both overlap before commit
            await session.commit()

    await asyncio.gather(attempt(), attempt())
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(NewsRecoveryCheckpoint).where(
            NewsRecoveryCheckpoint.workspace_id == workspace_id))
    assert count == 1
