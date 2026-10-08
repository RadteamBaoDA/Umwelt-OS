"""Owner-local fictional canonical relationship fixtures for the explicit P12 demo seed."""

from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.relationships.models import Relationship


async def _admit_seed(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Admit the owner workspace before inspecting or creating demo rows."""
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit seed workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def _entity_id(scope: Scope, key: str) -> UUID:
    """Return the workspace-derived ID the entity seed gives a fixture."""
    return uuid5(NAMESPACE_URL, f"bbd-os.demo.entity:{scope.workspace_id}:{p12_demo_seed_id('entity', key)}")


async def ensure_demo_relationships(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Link the seeded fictional archivist to the project without overwriting existing facts.

    The coordinator owns the encompassing transaction and durable receipt. This helper flushes
    only; a completed receipt prevents later runs from resurrecting a hard-deleted relationship.
    """
    await _admit_seed(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    relationship_id = uuid5(NAMESPACE_URL, "bbd-os.demo.relationship:{}:{}".format(
        scope.workspace_id, p12_demo_seed_id("relationship", "mira-coordinates-orchard-catalogue"),
    ))
    if await session.scalar(select(Relationship.id).where(
        Relationship.id == relationship_id, Relationship.workspace_id == scope.workspace_id,
    )) is not None:
        return 0, 1
    session.add(Relationship(
        id=relationship_id,
        workspace_id=scope.workspace_id,
        # Entity fixture IDs are workspace-derived (see entities.seed.ensure_demo_entities).
        source_entity_id=_entity_id(scope, "mira-archivist"),
        target_entity_id=_entity_id(scope, "orchard-project"),
        type="COORDINATES",
        origin="owner",
        confidence=1.0,
        metadata_json={"demo_namespace": "bbd-os.demo.phase-12"},
    ))
    await session.flush()
    return 1, 0
