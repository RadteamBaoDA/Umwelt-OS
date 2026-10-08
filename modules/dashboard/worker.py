"""ARQ-owned schedules for daily briefs and bounded dashboard highlight evaluation."""

import logging
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import worker_cursors
from core.config import Settings
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope
from modules.dashboard import briefs, public
from modules.dashboard.daily_schemas import BriefSchedule
from modules.dashboard.models import BriefSchedule as BriefScheduleRow
from modules.dashboard.models import GadgetDefinition
from modules.settings import public as settings_public

HIGHLIGHT_CURSOR_KEY = "dashboard:highlights:workspace-cursor"
BRIEF_CURSOR_KEY = "dashboard:briefs:workspace-cursor"


_log = logging.getLogger(__name__)
CURSOR_STATE_KEY = worker_cursors.STATE_KEY
_CURSOR_KEYS = frozenset({HIGHLIGHT_CURSOR_KEY, BRIEF_CURSOR_KEY})


async def _read_workspace_cursor(ctx: dict[str, object], key: str) -> UUID | None:
    """Read a fixed identity cursor through the shared stale-write-guarded cursor store."""
    return await worker_cursors.read_cursor(ctx, key, _CURSOR_KEYS)


async def _write_workspace_cursor(ctx: dict[str, object], key: str, cursor: UUID | None) -> None:
    """Write a fixed identity cursor through the shared stale-write-guarded cursor store."""
    await worker_cursors.write_cursor(ctx, key, cursor, _CURSOR_KEYS)


async def run_scheduled_highlights(ctx: dict[str, object]) -> int:
    """Evaluate at most ten definitions globally from a fair ordered identity page.

    The identity cursor advances past denied subjects as well as successful visits, so unavailable
    owners cannot pin the scan or let a later definition starve. A 100-ID discovery bound and
    ten-evaluation execution bound rotate by definition ID. Each selected workspace gets a fresh
    session and current Recipe W admission; Redis is only an optimization over the shared
    ``ctx['w2_cursor_state']`` object installed at worker startup.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    cursor = await _read_workspace_cursor(ctx, HIGHLIGHT_CURSOR_KEY)
    async with factory() as session:
        statement = select(GadgetDefinition.id, GadgetDefinition.workspace_id, GadgetDefinition.owner_id).where(
            GadgetDefinition.renderer.in_(("highlights", "watch_rules")),
            GadgetDefinition.highlight_rules != [], GadgetDefinition.source_ids != [],
        )
        if cursor is not None:
            statement = statement.where(GadgetDefinition.id > cursor)
        candidates = list((await session.execute(
            statement.order_by(GadgetDefinition.id).limit(100)
        )).all())
    if not candidates:
        await _write_workspace_cursor(ctx, HIGHLIGHT_CURSOR_KEY, None)
        return 0

    settings = cast(Settings, ctx["settings"])
    evaluated = 0
    last_consumed_id: UUID | None = None
    for definition_id, workspace_id, owner_id in candidates:
        if evaluated >= 10:
            break
        last_consumed_id = definition_id
        async with factory() as session:
            try:
                owner = await workspaces.resolve_workspace_owner_context(
                    session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if owner is None or owner.user_id != owner_id:
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                await workspaces.read_access_fence(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if not await settings_public.module_is_enabled(
                    session, "dashboard", scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                ):
                    continue
                await public.evaluate_gadget_highlights(
                    session, definition_id, emit_notifications=True, scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                evaluated += 1
            except HTTPException as exc:
                if exc.status_code in {401, 403, 404, 409}:
                    await session.rollback()
                    continue
                raise
    await _write_workspace_cursor(ctx, HIGHLIGHT_CURSOR_KEY, last_consumed_id)
    return evaluated


async def run_scheduled_brief(ctx: dict[str, object]) -> bool:
    """Preserve default enabled schedules with a fair ten-workspace initialization bound.

    Bounded Workspace identity discovery includes task-only and empty workspaces. Missing schedule
    rows receive the existing default under the original owner fence; disabled and automation-owned
    rows are preserved. Each page visits at most ten candidates in separate sessions, and denied or
    disabled subjects still advance the rotating cursor.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    settings = cast(Settings, ctx["settings"])
    cursor = await _read_workspace_cursor(ctx, BRIEF_CURSOR_KEY)
    async with factory() as session:
        workspace_ids = await workspaces.list_workspace_job_candidate_ids(
            session, after=cursor, limit=100,
        )
    if not workspace_ids:
        await _write_workspace_cursor(ctx, BRIEF_CURSOR_KEY, None)
        return False

    visit_ids = workspace_ids[:10]
    created_any = False
    for workspace_id in visit_ids:
        async with factory() as session:
            try:
                owner = await workspaces.resolve_workspace_owner_context(
                    session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if owner is None:
                    continue
                scope = InternalJobScope(
                    workspace_id=workspace_id, actor_user_id=owner.user_id,
                    membership_revision=owner.membership_revision,
                )
                access_fence = await briefs._admit(
                    session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                if not await settings_public.module_is_enabled(
                    session, "dashboard", scope=scope,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                ):
                    continue
                access_fence = await workspaces.lock_access_fence(
                    session, scope=scope, expected=access_fence,
                    multi_workspace_enabled=settings.multi_workspace_enabled,
                )
                await briefs._lock_schedule_slot(session, workspace_id)
                schedule_row = await session.scalar(select(BriefScheduleRow).where(
                    BriefScheduleRow.workspace_id == workspace_id,
                    BriefScheduleRow.owner_id == owner.user_id,
                ).with_for_update())
                if schedule_row is None:
                    default = BriefSchedule()
                    schedule_row = BriefScheduleRow(
                        workspace_id=workspace_id, owner_id=owner.user_id,
                        enabled=default.enabled, hour=default.hour, minute=default.minute,
                        timezone=default.timezone, schedule_owner="internal_brief", automation_id=None,
                    )
                    session.add(schedule_row)
                    await session.flush()
                    schedule = default
                    should_run = schedule.enabled
                    await commit_with_replay(
                        session, [], scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                        access_fence=access_fence,
                    )
                else:
                    schedule = BriefSchedule.model_validate(schedule_row)
                    should_run = schedule.enabled and schedule_row.schedule_owner == "internal_brief"
                    await session.rollback()  # release the schedule lock before possible model I/O
                if should_run:
                    created = await briefs.run_due_brief(
                        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                        settings=settings, redis=cast(Redis, ctx["redis"]),
                    )
                    created_any = created is not None or created_any
            except HTTPException as exc:
                if exc.status_code in {401, 403, 404, 409}:
                    await session.rollback()
                    continue
                raise
    await _write_workspace_cursor(ctx, BRIEF_CURSOR_KEY, visit_ids[-1])
    return created_any
