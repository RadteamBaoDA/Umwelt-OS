"""ARQ-owned internal schedule for the daily brief (07:00 Asia/Ho_Chi_Minh unless the owner edits it)."""

from typing import cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from modules.dashboard import briefs

OWNER_ID = 1  # single-owner deployment; matches settings.public.OWNER_ID


async def run_scheduled_brief(ctx: dict[str, object]) -> bool:
    """Create today's brief once the schedule time has passed; run every minute and once at startup.

    The startup run is the single catch-up for the *current* day only. Returns True when a new
    revision was saved. Model outages are swallowed by ``run_due_brief`` (15-minute cooldown) so the
    previous brief stays untouched and the job never retries in a tight loop.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        created = await briefs.run_due_brief(
            session, OWNER_ID, settings=cast(Settings, ctx["settings"]), redis=cast(Redis, ctx["redis"])
        )
        return created is not None
