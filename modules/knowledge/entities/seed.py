"""Owner-local fictional knowledge entity fixtures for the explicit P12 demo seed."""

from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.entities.models import Entity
from modules.knowledge.entities.schemas import canonicalize_name


async def _admit_seed(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Admit the owner workspace before inspecting or creating demo entities."""
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit entity seed workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def ensure_demo_entities(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Create stable fictional project/person entities, preserving existing owner edits and tombstones.

    The P12 coordinator holds the owner-workspace seed lock and transaction. IDs are stable within
    each workspace; existing rows are never updated, and the coordinator receipt prevents
    hard-deleted fixtures from being recreated after a completed seed.
    """
    await _admit_seed(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    seeds = (
        ("project", "Orchard Lantern Catalogue", "A fictional project documenting the north orchard lanterns.", "orchard-project"),
        ("person", "Mira Nguyen", "A fictional archivist coordinating the catalogue.", "mira-archivist"),
    )
    created = existing = 0
    for entity_type, name, description, key in seeds:
        seed_key = p12_demo_seed_id("entity", key)
        entity_id = uuid5(NAMESPACE_URL, f"bbd-os.demo.entity:{scope.workspace_id}:{seed_key}")
        if await session.scalar(select(Entity.id).where(
            Entity.id == entity_id, Entity.workspace_id == scope.workspace_id,
        )) is not None:
            existing += 1
            continue
        session.add(Entity(
            workspace_id=scope.workspace_id, id=entity_id, type=entity_type, name=name, canonical_name=canonicalize_name(name),
            description=description, name_origin="owner", description_origin="owner",
            metadata_json={"demo_namespace": "bbd-os.demo.phase-12"}, revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
