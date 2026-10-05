"""ARQ entry points for automation dispatch; PostgreSQL stays the queue authority."""

from typing import Any, cast

from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.automations import execution, producers, scheduler


async def reconcile_automation_runs(ctx: dict[str, Any]) -> int:
    """Every few seconds: fire due schedule slots, sweep trigger producers, fan out inbox triggers, then (re)enqueue runs.

    Order matters: slots and triggers create ``queued`` rows first so the same pass can dispatch
    them. Every step is idempotent (run identity, inbox status), so overlapping or restarted
    passes only repeat work that the database already absorbs. Returns jobs enqueued.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    from modules.settings.public import module_is_enabled

    async with factory() as session:
        enabled = await module_is_enabled(session, "automations")
    if not enabled:
        return 0
    await scheduler.tick(factory)
    await producers.sweep(factory)
    await execution.fan_out_triggers(factory)
    return await execution.dispatch_runs(factory, cast(ArqRedis, ctx["redis"]))


async def process_automation_run(ctx: dict[str, Any], run_id: str) -> str:
    """Run one dispatched automation run to its next stop (done, approval wait, retry or review)."""
    return await execution.process_run(ctx, run_id)
