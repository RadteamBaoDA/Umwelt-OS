"""Bounded durable cleanup for expired agent traces and temporary browser evidence."""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.agents.public import redact_expired_agent_traces
from modules.observability.models import MaintenanceSummary
from modules.settings import public as settings_public
from modules.tools.public import purge_expired_browser_evidence


async def run_retention_maintenance(ctx: dict[str, object]) -> int:
    """Process one bounded retention pass and persist its latest non-empty result.

    Canonical run outcomes, approval/effect IDs, browser idempotency tombstones, source data,
    document history, and Chat messages remain owned by their original modules.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    now = datetime.now(UTC)
    async with factory() as session:
        policy = await settings_public.read_retention_settings(session)
        cutoff = now - timedelta(days=policy.agent_trace_days)
        traces = await redact_expired_agent_traces(session, cutoff=cutoff, limit=100)
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
