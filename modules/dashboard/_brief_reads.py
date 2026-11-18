"""Workspace-scoped point reads of saved briefs for other modules (chat task threads, automations)."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import Scope
from modules.dashboard.briefs import _admit, _with_live_status
from modules.dashboard.daily_schemas import BriefRead
from modules.dashboard.models import DailyBrief

# Bound on ids per read; matches the lock-page bound passed to ``_with_live_status``.
MAX_BRIEF_IDS = 500


async def _rows(
    session: AsyncSession, brief_ids: Sequence[UUID], *, scope: Scope, multi_workspace_enabled: bool
) -> list[DailyBrief]:
    """Load the caller's own saved briefs among ``brief_ids`` (workspace and owner scoped)."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    ids = list(dict.fromkeys(brief_ids))[:MAX_BRIEF_IDS]
    if not ids:
        return []
    rows = await session.scalars(
        select(DailyBrief).where(
            DailyBrief.id.in_(ids),
            DailyBrief.workspace_id == scope.workspace_id,
            DailyBrief.owner_id == _actor(scope),
        )
    )
    return list(rows)


async def get_brief(
    session: AsyncSession, brief_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> BriefRead | None:
    """Return one saved brief with read-time liveness, or None when absent or out of scope.

    A brief whose captured inputs no longer match comes back ``status="stale"`` with blank content.
    Never writes.
    """
    rows = await _rows(
        session, [brief_id], scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if not rows:
        return None
    return (
        await _with_live_status(
            session,
            rows,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            max_briefs=MAX_BRIEF_IDS,
        )
    )[0]


async def live_brief_ids(
    session: AsyncSession,
    brief_ids: Sequence[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> set[UUID]:
    """Return which of ``brief_ids`` are saved, in scope and still eligible to be shown.

    Missing, foreign or stale briefs (a captured source deleted, paused or made local-only, or a
    changed support snapshot) are absent from the result, so callers fail closed. At most
    ``MAX_BRIEF_IDS`` distinct ids are considered; ids beyond that are dropped and therefore also
    fail closed (reported as not live). Takes share locks on captured Sources/Documents in one
    canonical-order acquisition; never writes rows.
    """
    rows = await _rows(
        session, brief_ids, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    # One ordered lock acquisition for all (<= MAX_BRIEF_IDS) rows; see prelock_captured_inputs.
    live = await _with_live_status(
        session,
        rows,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        max_briefs=MAX_BRIEF_IDS,
    )
    return {b.id for b in live if b.status == "current"}
