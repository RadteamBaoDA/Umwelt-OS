"""Owner-local fictional Phase 8 goal fixtures and linked milestone records."""

from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import demo_seed_id
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext
from modules.goals.models import Goal


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


async def ensure_demo_goals(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Add missing owner goals with stable milestone/task references and return created/existing counts.

    The CLI coordinator supplies the owner workspace scope. Existing rows are read
    workspace-scoped and left untouched, including revisions and edits; a durable coordinator receipt
    prevents this function from running again after hard deletion. This function flushes but never
    commits so the linked tasks and completion receipt remain one transaction.
    """
    await _admit_seed(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    created = 0
    existing = 0
    for seed in GOAL_SEEDS:
        goal_id = _ws_id(scope, seed["id"])
        found = await session.scalar(select(Goal.id).where(
            Goal.id == goal_id, Goal.workspace_id == scope.workspace_id,
        ))
        if found is not None:
            existing += 1
            continue
        session.add(Goal(
            id=goal_id, workspace_id=scope.workspace_id, owner_id=_actor(scope), title=seed["title"],
            description=seed["description"], desired_outcome=seed["desired_outcome"],
            deadline=seed["deadline"], progress=seed["progress"],
            manual_progress=False, status="active", milestones=[
                {**item, "id": str(_ws_id(scope, item["id"])),
                 "task_id": str(_ws_id(scope, item["task_id"])) if item["task_id"] else None}
                for item in seed["milestones"]
            ],
            entity_ids=[], accepted_proposals=[], revision=1,
        ))
        created += 1
    await session.flush()
    return created, existing
