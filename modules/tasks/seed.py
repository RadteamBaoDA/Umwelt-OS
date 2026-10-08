"""Owner-local fictional Phase 8 task seed records."""

from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext
from modules.tasks.models import Task


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


def _actor(scope: Scope) -> int:
    """Return the principal recorded by a real workspace or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


def _ws_id(scope: Scope, seed_id: UUID | str) -> UUID:
    """Derive a workspace-local stable ID so one workspace's fixtures never collide with another's."""
    return uuid5(NAMESPACE_URL, f"bbd-os.demo.seed:{scope.workspace_id}:{seed_id}")


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


async def ensure_demo_tasks(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Add missing fictional tasks for the trusted owner and return created/existing counts.

    The caller supplies the owner workspace scope and owns the outer transaction; IDs are stable per workspace.
    Existing live or tombstoned owner rows keep their edits and revision; each insert is flushed
    for the linked goal writes but this function never commits caller work. Rows are matched by workspace, never adopted across workspaces.
    """
    await _admit_seed(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    created = 0
    existing = 0
    for seed in TASK_SEEDS:
        task_id = _ws_id(scope, seed["id"])
        found = await session.scalar(select(Task.id).where(
            Task.id == task_id, Task.workspace_id == scope.workspace_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Task(
            id=task_id, workspace_id=scope.workspace_id, owner_id=_actor(scope), title=seed["title"],
            description=seed["description"], status=seed["status"],
            due_date=seed["due_date"], due_at=None,
            completed_at=seed["completed_at"], goal_id=_ws_id(scope, seed["goal_id"]) if seed["goal_id"] else None,
            entity_ids=[], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
