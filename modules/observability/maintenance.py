"""Bounded durable cleanup for expired agent traces and temporary browser evidence."""

import logging
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import worker_cursors
from core.config import Settings
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope
from modules.agents import public as agents
from modules.observability.models import MaintenanceSummary
from modules.settings import public as settings_public
from modules.tools.public import purge_expired_browser_evidence

TRACE_CURSOR_KEY = "observability:retention:workspace-cursor"
_CURSOR_KEYS = frozenset({TRACE_CURSOR_KEY})
_TRACE_BUDGET = 100
_MAX_PAGES = 3  # bounded discovery per hourly pass; the cursor carries the rest to the next pass
logger = logging.getLogger(__name__)


async def _redact_workspace_traces(
    factory: async_sessionmaker[AsyncSession], workspace_id: UUID, *, now: datetime, limit: int,
    multi_workspace_enabled: bool,
) -> int:
    """Redact one workspace's expired traces in its own session under its owner's current fence.

    Retention is cleanup: it is never module-gated ("observability" is an instance module, so
    module_is_enabled would always deny it). Recipe W: the owner and revision come from the live workspace, never a captured epoch.
    Lost admission or retention policy denial skips the workspace
    and leaves its durable rows untouched. Commits only after the workspace pass succeeds.
    """
    async with factory() as session:
        try:
            owner = await workspaces.resolve_workspace_owner_context(
                session, workspace_id, multi_workspace_enabled=multi_workspace_enabled,
            )
            if owner is None:
                return 0
            scope = InternalJobScope(
                workspace_id=owner.workspace_id, actor_user_id=owner.user_id,
                membership_revision=owner.membership_revision,
            )
            await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
            policy = await settings_public.read_retention_settings(
                session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            redacted = await agents.redact_expired_agent_traces(
                session, cutoff=now - timedelta(days=policy.agent_trace_days), limit=limit,
                scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            )
            await session.commit()
            return redacted
        except HTTPException as exc:
            if exc.status_code in {401, 403, 404, 409}:
                await session.rollback()
                return 0
            raise


async def run_retention_maintenance(ctx: dict[str, object]) -> int:
    """Process one bounded retention pass and persist its latest non-empty result.

    Agent traces are redacted one workspace session at a time over a fair, cursor-paged identity
    list (100 traces per pass in total). Browser evidence expiry is a deliberate instance-wide pass
    that reads no content, and the MaintenanceSummary singleton stays global. Canonical run
    outcomes, approval/effect IDs, browser idempotency tombstones, source data, document history,
    and Chat messages remain owned by their original modules.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    now = datetime.now(UTC)
    cursor = await worker_cursors.read_cursor(ctx, TRACE_CURSOR_KEY, _CURSOR_KEYS)
    traces = 0
    last = cursor
    exhausted = False
    try:
        for _ in range(_MAX_PAGES):
            if traces >= _TRACE_BUDGET:
                break
            async with factory() as session:
                page = await agents.list_agent_trace_workspace_ids(session, after=last, limit=100)
            if not page:
                exhausted = True
                break
            for workspace_id in page:
                if traces >= _TRACE_BUDGET:
                    break
                last = workspace_id
                try:
                    traces += await _redact_workspace_traces(
                        factory, workspace_id, now=now, limit=_TRACE_BUDGET - traces, multi_workspace_enabled=enabled,
                    )
                except Exception:
                    logger.warning("agent trace retention failed for workspace %s", workspace_id, exc_info=True)
    except AttributeError:
        # Agents trace listing not available in this build: skip the trace phase, keep evidence purge.
        logger.warning("agent trace retention unavailable; skipping trace phase", exc_info=True)
    await worker_cursors.write_cursor(ctx, TRACE_CURSOR_KEY, None if exhausted else last, _CURSOR_KEYS)
    async with factory() as session:
        evidence = await purge_expired_browser_evidence(session, limit=200)
        summary = await session.scalar(
            select(MaintenanceSummary).where(MaintenanceSummary.id == 1).with_for_update()
        )
        if traces or evidence or summary is None or summary.next_eligible_at is None or summary.next_eligible_at <= now:
            if summary is None:
                summary = MaintenanceSummary(id=1)
                session.add(summary)
            summary.completed_at = now
            summary.agent_traces_redacted = traces
            summary.temporary_data_deleted = evidence
            summary.next_eligible_at = now + timedelta(hours=1)
        await session.commit()
        return traces + evidence
