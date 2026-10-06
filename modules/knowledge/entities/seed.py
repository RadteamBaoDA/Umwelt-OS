"""Owner-local fictional knowledge entity fixtures for the explicit P12 demo seed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from modules.knowledge.entities.models import Entity
from modules.knowledge.entities.schemas import canonicalize_name


async def ensure_demo_entities(session: AsyncSession) -> tuple[int, int]:
    """Create stable fictional project/person entities, preserving existing owner edits and tombstones.

    The P12 coordinator holds the singleton-owner seed lock and transaction. IDs are stable across
    attempts; existing rows are never updated, and the coordinator receipt prevents hard-deleted
    fixtures from being recreated after a completed seed.
    """
    seeds = (
        ("project", "Orchard Lantern Catalogue", "A fictional project documenting the north orchard lanterns.", "orchard-project"),
        ("person", "Mira Nguyen", "A fictional archivist coordinating the catalogue.", "mira-archivist"),
    )
    created = existing = 0
    for entity_type, name, description, key in seeds:
        entity_id = p12_demo_seed_id("entity", key)
        if await session.scalar(select(Entity.id).where(Entity.id == entity_id)) is not None:
            existing += 1
            continue
        session.add(Entity(
            id=entity_id, type=entity_type, name=name, canonical_name=canonicalize_name(name),
            description=description, name_origin="owner", description_origin="owner",
            metadata_json={"demo_namespace": "bbd-os.demo.phase-12"}, revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
