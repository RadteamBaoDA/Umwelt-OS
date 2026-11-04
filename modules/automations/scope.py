"""Workspace-scope helpers shared by the automations modules (kept apart so public/execution can both import them)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext

DENIED_STATUSES = frozenset({401, 403, 404, 409})  # admission denials that skip one workspace in workers


def _actor(scope: Scope) -> int:
    """Return the acting owner ID: the stamped actor of a job scope or the request user."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


def _require_owner(scope: Scope) -> None:
    """Require an explicit workspace scope and, for requests, the workspace owner role.

    Raises ``TypeError`` for an untyped scope and ``ForbiddenError`` (403) for a member; job scopes
    were admitted against the stamped owner already. Pure; runs before any query.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Owner admission; a member is denied before any session await."""
    _require_owner(scope)
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
