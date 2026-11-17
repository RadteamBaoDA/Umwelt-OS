"""Workspace-scope helpers shared by the automations modules (kept apart so public/execution can both import them)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import actor_id as _actor
from core.workspaces.admission import admit_scope
from core.workspaces.admission import require_workspace_owner as _require_owner
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext

__all__ = ["DENIED_STATUSES", "_actor", "_admit", "_require_owner"]

DENIED_STATUSES = frozenset(
    {401, 403, 404, 409}
)  # admission denials that skip one workspace in workers


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Owner admission; a member is denied before any session await (canonical ``admit_scope``)."""
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        allowed_scopes=(WorkspaceContext, InternalJobScope),
    )
