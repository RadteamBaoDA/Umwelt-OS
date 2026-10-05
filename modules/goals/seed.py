"""Owner-local fictional Phase 8 goal fixtures and linked milestone records."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from modules.goals.models import Goal

GOAL_SEEDS = (
    {
        "id": demo_seed_id("goal", "orchard-lanterns"),
        "title": "Catalogue Orchard Lanterns",
        "description": "Survey and document the historical lantern collection across the north orchard.",
        "desired_outcome": "Complete verified catalogue index with photographs and inscriptions before the harvest festival.",
        "deadline": date(2026, 10, 15),
        "progress": 33.3,
        "milestones": [
            {
                "id": str(demo_seed_id("milestone", "orchard-survey")),
                "title": "Survey north grove lanterns",
                "completed": True,
                "due_date": "2026-09-20",
                "order": 0,
                "task_id": str(demo_seed_id("task", "survey-grove")),
            },
            {
                "id": str(demo_seed_id("milestone", "orchard-photos")),
                "title": "Photograph lantern inscriptions",
                "completed": False,
                "due_date": "2026-10-05",
                "order": 1,
                "task_id": str(demo_seed_id("task", "photo-inscriptions")),
            },
            {
                "id": str(demo_seed_id("milestone", "orchard-catalogue")),
                "title": "Draft field notes and catalogue index",
                "completed": False,
                "due_date": "2026-10-12",
                "order": 2,
                "task_id": None,
            },
        ],
    },
    {
        "id": demo_seed_id("goal", "paper-boats"),
        "title": "Village Paper Boat Workshop",
        "description": "Organize community folding workshop and floating lantern ceremony.",
        "desired_outcome": "Host community workshop with 50 folding guides and mulberry paper boats.",
        "deadline": date(2026, 10, 20),
        "progress": 0.0,
        "milestones": [
            {
                "id": str(demo_seed_id("milestone", "boats-paper")),
                "title": "Source waterproof origami mulberry paper",
                "completed": False,
                "due_date": "2026-10-08",
                "order": 0,
                "task_id": str(demo_seed_id("task", "order-mulberry")),
            },
            {
                "id": str(demo_seed_id("milestone", "boats-guide")),
                "title": "Prepare illustrated folding guides",
                "completed": False,
                "due_date": "2026-10-14",
                "order": 1,
                "task_id": None,
            },
        ],
    },
)


async def ensure_demo_goals(session: AsyncSession, owner_id: int) -> tuple[int, int]:
    """Add missing owner goals with stable milestone/task references and return created/existing counts.

    The authenticated singleton owner ID comes from the CLI coordinator. Existing rows are read
    owner-scoped and left untouched, including revisions and edits; a durable coordinator receipt
    prevents this function from running again after hard deletion. This function flushes but never
    commits so the linked tasks and completion receipt remain one transaction.
    """
    created = 0
    existing = 0
    for seed in GOAL_SEEDS:
        found = await session.scalar(select(Goal.id).where(
            Goal.id == seed["id"], Goal.owner_id == owner_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Goal(
            id=seed["id"], owner_id=owner_id, title=seed["title"],
            description=seed["description"], desired_outcome=seed["desired_outcome"],
            deadline=seed["deadline"], progress=seed["progress"],
            manual_progress=False, status="active", milestones=seed["milestones"],
            entity_ids=[], accepted_proposals=[], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
