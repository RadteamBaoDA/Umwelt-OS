"""Owner/config admission helpers shared by every Connector public operation.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    ForbiddenError,
)
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.connectors.models import (
    ConnectorProvisioning,
)
from modules.sources.public import ConnectorSource, SourceFence


def _connector_actor(scope: Scope) -> int:
    """Require an owner principal before querying private Connector metadata or credentials."""
    if isinstance(scope, WorkspaceContext):
        if scope.role != "owner":
            raise ForbiddenError("Workspace owner required")
        return scope.user_id
    if isinstance(scope, InternalJobScope):
        return scope.actor_user_id
    raise ValueError("A verified workspace principal is required")


async def _connector_access(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence | None = None,
) -> AccessFence:
    """Freshly check owner/config admission without acquiring earlier transaction locks."""
    _connector_actor(scope)
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("The actual configured workspace flag is required")
    current = await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if access_fence is not None and current != access_fence:
        raise ConflictError("Connector access fence changed")
    return current


async def _read_scoped_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence | None = None,
    source_fence: SourceFence | None = None,
) -> ConnectorSource | None:
    """Read live Source through its owner and compare exact supplied held-lock snapshots.

    This acquires no row locks. A DTO or binding digest alone is never admission; internal
    subjects keep their original Source generation. Callers that mutate retain the ordered
    account/workspace/Source locks until their final commit.
    """
    from modules.sources import public as sources

    await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
    source = await sources.get_connector_source(
        session,
        source_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if source_fence is not None:
        current = await sources.get_source_fence(
            session,
            source_id,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if current != source_fence or source_fence.workspace_id != scope.workspace_id:
            raise ConflictError("Connector Source fence changed")
    return source


async def observability_queue_summary(
    session: AsyncSession, *, instance_operator: bool
) -> dict[str, dict[str, int]]:
    """Return operator-only instance provisioning counts without workflow or credential data."""
    if instance_operator is not True:
        raise PermissionError("Instance operator admission required")
    counts = dict(
        (
            await session.execute(
                select(ConnectorProvisioning.state, func.count()).group_by(
                    ConnectorProvisioning.state
                )
            )
        ).all()
    )
    return {"connector_provisioning": counts}
