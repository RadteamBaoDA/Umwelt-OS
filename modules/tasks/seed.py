"""Owner-local fictional Phase 8 task seed records."""

from datetime import UTC, date, datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from modules.tasks.models import Task

TASK_SEEDS = (
    {
        "id": demo_seed_id("task", "survey-grove"),
        "title": "Survey north grove lanterns",
        "description": "Map locations of all stone lanterns in the north orchard.",
        "status": "done",
        "due_date": date(2026, 9, 20),
        "completed_at": datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
        "goal_id": demo_seed_id("goal", "orchard-lanterns"),
    },
    {
        "id": demo_seed_id("task", "photo-inscriptions"),
        "title": "Photograph lantern inscriptions",
        "description": "Take high-resolution macro photos of seasonal poetry inscriptions.",
        "status": "todo",
        "due_date": date(2026, 10, 5),
        "completed_at": None,
        "goal_id": demo_seed_id("goal", "orchard-lanterns"),
    },
    {
        "id": demo_seed_id("task", "archival-paper"),
        "title": "Purchase archival paper for field notes",
        "description": "Acid-free paper for binding the lantern catalogue notes.",
        "status": "inbox",
        "due_date": None,
        "completed_at": None,
        "goal_id": demo_seed_id("goal", "orchard-lanterns"),
    },
    {
        "id": demo_seed_id("task", "order-mulberry"),
        "title": "Order mulberry paper sheets",
        "description": "Fifty large sheets of water-resistant washi for boat folding.",
        "status": "todo",
        "due_date": date(2026, 10, 8),
        "completed_at": None,
        "goal_id": demo_seed_id("goal", "paper-boats"),
    },
    {
        "id": demo_seed_id("task", "reserve-pavilion"),
        "title": "Reserve community pavilion",
        "description": "Confirm weekend reservation with the village cultural board.",
        "status": "blocked",
        "due_date": date(2026, 10, 4),
        "completed_at": None,
        "goal_id": demo_seed_id("goal", "paper-boats"),
    },
    {
        "id": demo_seed_id("task", "review-tea-harvest"),
        "title": "Review tea harvest schedule",
        "description": "Check upcoming autumn harvest timeline for conflicting dates.",
        "status": "inbox",
        "due_date": None,
        "completed_at": None,
        "goal_id": None,
    },
    {
        "id": demo_seed_id("task", "prepare-release"),
        "title": "Prepare release",
        "description": "Prepare release documentation and verify build artifacts.",
        "status": "in_progress",
        "due_date": date(2026, 9, 25),
        "completed_at": None,
        "goal_id": None,
    },
)


async def ensure_demo_tasks(session: AsyncSession, owner_id: int) -> tuple[int, int]:
    """Add missing fictional tasks for the trusted owner and return created/existing counts.

    The caller supplies the authenticated singleton owner ID and owns the outer transaction.
    Existing live or tombstoned owner rows keep their edits and revision; each insert is flushed
    for the linked goal writes but this function never commits caller work. A deterministic ID
    occupied by another owner fails at the primary key instead of being adopted.
    """
    created = 0
    existing = 0
    for seed in TASK_SEEDS:
        found = await session.scalar(select(Task.id).where(
            Task.id == seed["id"], Task.owner_id == owner_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Task(
            id=seed["id"], owner_id=owner_id, title=seed["title"],
            description=seed["description"], status=seed["status"],
            due_date=seed["due_date"], due_at=None,
            completed_at=seed["completed_at"], goal_id=seed["goal_id"],
            entity_ids=[], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
