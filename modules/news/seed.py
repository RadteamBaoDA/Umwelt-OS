"""Owner-local fictional Phase 8 topic fixtures."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from modules.news.topics import Topic

TOPIC_SEEDS = (
    {
        "id": demo_seed_id("topic", "papercraft"),
        "name": "Traditional Papercraft",
        "keywords": ["origami", "washi", "mulberry paper", "folding techniques"],
        "weight": 1.5,
    },
    {
        "id": demo_seed_id("topic", "orchards"),
        "name": "Historical Orchards & Architecture",
        "keywords": ["lanterns", "stone carving", "orchard heritage", "inscriptions"],
        "weight": 1.2,
    },
)


async def ensure_demo_topics(session: AsyncSession, owner_id: int) -> tuple[int, int]:
    """Add missing owner topics and return created/existing counts without committing.

    The coordinator supplies its authenticated singleton owner ID and holds the shared P08 seed
    transaction. Owner-scoped identity checks include tombstones and preserve all existing profile
    edits/revisions; the completion receipt prevents later calls from restoring hard-deleted rows.
    """
    created = 0
    existing = 0
    for seed in TOPIC_SEEDS:
        found = await session.scalar(select(Topic.id).where(
            Topic.id == seed["id"], Topic.owner_id == owner_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Topic(
            id=seed["id"], owner_id=owner_id, name=seed["name"],
            keywords=seed["keywords"], entity_ids=[], is_active=True,
            weight=seed["weight"], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
