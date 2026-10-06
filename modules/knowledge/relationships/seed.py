"""Owner-local fictional canonical relationship fixtures for the explicit P12 demo seed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from modules.knowledge.relationships.models import Relationship


async def ensure_demo_relationships(session: AsyncSession) -> tuple[int, int]:
    """Link the seeded fictional archivist to the project without overwriting existing facts.

    The coordinator owns the encompassing transaction and durable receipt. This helper flushes
    only; a completed receipt prevents later runs from resurrecting a hard-deleted relationship.
    """
    relationship_id = p12_demo_seed_id("relationship", "mira-coordinates-orchard-catalogue")
    if await session.scalar(select(Relationship.id).where(Relationship.id == relationship_id)) is not None:
        return 0, 1
    session.add(Relationship(
        id=relationship_id,
        source_entity_id=p12_demo_seed_id("entity", "mira-archivist"),
        target_entity_id=p12_demo_seed_id("entity", "orchard-project"),
        type="COORDINATES",
        origin="owner",
        confidence=1.0,
        metadata_json={"demo_namespace": "bbd-os.demo.phase-12"},
    ))
    await session.flush()
    return 1, 0
