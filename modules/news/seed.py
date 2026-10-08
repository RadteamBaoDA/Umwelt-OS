"""Owner-local fictional Phase 8 topic fixtures."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from core.workspaces.schemas import Scope
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


async def ensure_demo_topics(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Add missing owner topics and return created/existing counts without committing.

    The coordinator supplies the admitted owner scope and holds the shared P08 seed
    transaction. Owner-scoped identity checks include tombstones and preserve all existing profile
    edits/revisions; the completion receipt prevents later calls from restoring hard-deleted rows.
    """
    created = 0
    existing = 0
    for seed in TOPIC_SEEDS:
        # Keep an existing pre-workspace fixture in place, but give each newly seeded
        # workspace its own deterministic primary key.
        workspace_seed_id = uuid5(NAMESPACE_URL, f"{seed['id']}/{scope.workspace_id}")
        found = await session.scalar(select(Topic.id).where(
            Topic.id.in_((seed["id"], workspace_seed_id)), Topic.workspace_id == scope.workspace_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Topic(
            id=workspace_seed_id, workspace_id=scope.workspace_id,
            owner_id=scope.user_id if hasattr(scope, "user_id") else scope.actor_user_id, name=seed["name"],
            keywords=seed["keywords"], entity_ids=[], is_active=True,
            weight=seed["weight"], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
