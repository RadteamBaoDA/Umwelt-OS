"""Owner-local fictional timeline fixtures for the explicit P12 demo seed."""

from datetime import UTC, date, datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from modules.timeline.models import Event


async def ensure_demo_events(session: AsyncSession) -> tuple[int, int]:
    """Create one stable fictional project event without changing an existing event.

    This seed-only owner contract flushes into the coordinator transaction and uses an owner-authored
    manual event with no fabricated source evidence. The P12 receipt prevents resurrection later.
    """
    event_id = p12_demo_seed_id("event", "lantern-catalogue-kickoff")
    if await session.scalar(select(Event.id).where(Event.id == event_id)) is not None:
        return 0, 1
    session.add(Event(
        id=event_id,
        type="project_milestone",
        title="Begin the orchard lantern catalogue",
        summary="Mira starts the fictional survey and inscription catalogue.",
        importance_score=0.6,
        confidence=1.0,
        metadata_json={"demo_namespace": "bbd-os.demo.phase-12"},
        origin="manual",
        date_precision="date",
        occurred_date=date(2026, 9, 20),
        observed_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
    ))
    await session.flush()
    return 1, 0
