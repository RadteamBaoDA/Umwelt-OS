"""Owner-local fictional timeline fixtures for the explicit P12 demo seed."""

from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext
from modules.timeline.models import Event


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


async def ensure_demo_events(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int]:
    """Create one stable fictional project event without changing an existing event.

    This seed-only owner contract flushes into the coordinator transaction and uses an owner-authored
    manual event with no fabricated source evidence. The P12 receipt prevents resurrection later.
    """
    await _admit_seed(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event_id = uuid5(NAMESPACE_URL, "bbd-os.demo.event:{}:{}".format(
        scope.workspace_id, p12_demo_seed_id("event", "lantern-catalogue-kickoff"),
    ))
    if await session.scalar(select(Event.id).where(
        Event.id == event_id, Event.workspace_id == scope.workspace_id,
    )) is not None:
        return 0, 1
    session.add(Event(
        id=event_id,
        workspace_id=scope.workspace_id,
        type="project_milestone",
        title="Begin the orchard lantern catalogue",
        summary="Mira starts the fictional survey and inscription catalogue.",
        importance_score=0.6,
        confidence=1.0,
        metadata_json={"demo_namespace": "bbd-os.demo.phase-12"},
        origin="manual",
        date_precision="date",
        occurred_date=date(2026, 9, 20),
        observed_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
    ))
    await session.flush()
    return 1, 0
