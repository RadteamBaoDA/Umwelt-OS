"""ARQ-owned schedules for daily briefs and bounded dashboard highlight evaluation."""

from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from modules.dashboard import briefs, public
from modules.dashboard.models import GadgetDefinition

OWNER_ID = 1  # single-owner deployment; matches settings.public.OWNER_ID
HIGHLIGHT_CURSOR_KEY = "dashboard:highlights:definition-cursor"


async def run_scheduled_highlights(ctx: dict[str, object]) -> int:
    """Evaluate up to ten rule-bearing definitions per minute with a rotating scan cursor.

    The scan runs independently of dashboard rendering. Notification keys bind the definition
    rule and immutable document version in PostgreSQL, so retries and repeated scans deduplicate.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    redis = cast(Redis, ctx["redis"])
    raw_cursor = await redis.get(HIGHLIGHT_CURSOR_KEY)
    if isinstance(raw_cursor, bytes):
        raw_cursor = raw_cursor.decode("ascii", errors="ignore")
    cursor = None
    if isinstance(raw_cursor, str):
        try:
            cursor = UUID(raw_cursor)
        except ValueError:
            cursor = None
    async with factory() as session:
        statement = select(GadgetDefinition.id, GadgetDefinition.owner_id).where(
            GadgetDefinition.renderer.in_(("highlights", "watch_rules")),
            GadgetDefinition.highlight_rules != [],
            GadgetDefinition.source_ids != [],
        )
        if cursor is not None:
            statement = statement.where(GadgetDefinition.id > cursor)
        rows = list((await session.execute(
            statement.order_by(GadgetDefinition.id).limit(10)
        )).all())
    if not rows:
        await redis.delete(HIGHLIGHT_CURSOR_KEY)
        return 0
    for definition_id, owner_id in rows:
        async with factory() as session:
            await public.evaluate_gadget_highlights(
                session, owner_id, definition_id, emit_notifications=True,
            )
    await redis.set(HIGHLIGHT_CURSOR_KEY, str(rows[-1][0]))
    return len(rows)


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
