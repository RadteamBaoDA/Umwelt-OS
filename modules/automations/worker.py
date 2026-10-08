"""ARQ entry points for automation dispatch; PostgreSQL stays the queue authority."""

import asyncio
import logging
from typing import Any, cast

from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import worker_cursors
from core.config import Settings
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope
from modules.automations import execution, producers, scheduler
from modules.automations.models import Automation

logger = logging.getLogger(__name__)
CURSOR_KEY = "automations_workspace"
ALLOWED = frozenset({CURSOR_KEY})
# ponytail: one page of PAGE workspaces per tick with a wrapping cursor; very large installs get slower ticks.
PAGE = 100


async def reconcile_automation_runs(ctx: dict[str, Any]) -> int:
    """Every few seconds: fire due schedule slots, sweep trigger producers, fan out inbox triggers, then (re)enqueue runs.

    Order matters: slots and triggers create ``queued`` rows first so the same pass can dispatch
    them. Every step is idempotent (run identity, inbox status), so overlapping or restarted
    passes only repeat work that the database already absorbs. Returns jobs enqueued.

    Discovery pages workspaces that still have any non-deleted automation (not only enabled ones) so
    queued runs and pending inbox rows of a just-disabled rule still drain. Each workspace runs in its
    own sessions under its owner's durable scope; an admission denial skips only that workspace.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    from modules.settings.public import module_is_enabled

    flag = cast(Settings, ctx["settings"]).multi_workspace_enabled
    after = await worker_cursors.read_cursor(ctx, CURSOR_KEY, ALLOWED)
    async with factory() as session:  # identity-only discovery from the root table
        ids = list(await session.scalars(
            select(Automation.workspace_id).where(
                Automation.deleted_at.is_(None), *([Automation.workspace_id > after] if after else []),
            ).distinct().order_by(Automation.workspace_id).limit(PAGE)))
    await worker_cursors.write_cursor(ctx, CURSOR_KEY, ids[-1] if len(ids) == PAGE else None, ALLOWED)
    enqueued = 0
    for workspace_id in ids:  # one session per step; never two workspace locks together
        try:
            async with factory() as session:
                owner = await workspaces.resolve_workspace_owner_context(
                    session, workspace_id, multi_workspace_enabled=flag)
                if owner is None:
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id, membership_revision=owner.membership_revision)
                if not await module_is_enabled(session, "automations", scope=scope, multi_workspace_enabled=flag):
                    continue
            await scheduler.tick(factory, scope=scope, multi_workspace_enabled=flag)
            await producers.sweep(factory, scope=scope, multi_workspace_enabled=flag)
            await execution.fan_out_triggers(factory, scope=scope, multi_workspace_enabled=flag)
            enqueued += await execution.dispatch_runs(
                factory, cast(ArqRedis, ctx["redis"]), scope=scope, multi_workspace_enabled=flag)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  # one failing workspace must not starve the rest of the pass
            logger.warning("automation reconcile skipped a workspace (%s)", type(exc).__name__)
    return enqueued


async def process_automation_run(ctx: dict[str, Any], run_id: str) -> str:
    """Run one dispatched automation run to its next stop (done, approval wait, retry or review)."""
    return await execution.process_run(ctx, run_id)
