"""Workspace admission helpers shared by the Agents module (Recipe J: captured original epoch).

An AgentRun stores its workspace, actor and the membership/configuration revisions that were
current when it was created. Execution, reads and publication rebuild an ``InternalJobScope``
from those durable columns and compare the live access fence with the stored pair. Legacy rows
with NULL epochs are quarantined (``run_epoch`` returns None); they are never rebased onto the
current epoch.
"""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext


class _EpochRow(Protocol):
    """Durable columns that carry an AgentRun's original workspace authorization."""

    workspace_id: UUID
    owner_id: int
    membership_revision: int | None
    configuration_revision: int | None


def actor(scope: Scope) -> int:
    """Return the principal recorded by a real workspace or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before any Agents query or lock.

    Members stay denied (403) before a statement runs; W3 sharing adds grants later.
    ``lock=True`` takes the access fence ahead of Source, Document and run locks.
    """
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit Agents workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def run_epoch(row: _EpochRow) -> tuple[InternalJobScope, AccessFence] | None:
    """Rebuild a run's original job scope and fence, or None for a legacy NULL-epoch row.

    Both revision columns are NULL together or positive together (database check), so one
    test covers both. The returned pair is built only from durable columns, never from a
    queued job argument or the current epoch.
    """
    if row.membership_revision is None or row.configuration_revision is None:
        return None
    scope = InternalJobScope(
        workspace_id=row.workspace_id, actor_user_id=row.owner_id,
        membership_revision=row.membership_revision,
    )
    original = AccessFence(
        workspace_id=row.workspace_id, user_id=row.owner_id,
        membership_revision=row.membership_revision,
        configuration_revision=row.configuration_revision,
    )
    return scope, original


async def admit_run(
    session: AsyncSession, row: _EpochRow, *, multi_workspace_enabled: bool, lock: bool = False,
) -> tuple[InternalJobScope, AccessFence] | None:
    """Admit a run's captured scope and require the live fence to equal the original epoch.

    Returns None for a quarantined legacy row. A revoked owner/membership or any changed
    revision raises the access fence's 401/404/409; callers treat these as terminal for the run
    and never retry under a newer epoch.
    """
    epoch = run_epoch(row)
    if epoch is None:
        return None
    scope, original = epoch
    fence = await admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        lock=lock, expected=original if lock else None,
    )
    if fence != original:
        raise HTTPException(status_code=409, detail="Workspace access fence changed")
    return scope, fence


def effective_workspace_modules(lifecycle: Any) -> Mapping[str, Any]:
    """Apply a workspace's explicit module disables to the build-time module map."""
    from core.modules import effective_modules, register_modules

    return effective_modules({item.id for item in lifecycle.modules if item.explicitly_disabled}, register_modules())


async def read_workspace_modules(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> Mapping[str, Any]:
    """Per-call module map for ``ToolRegistry.list_tools(modules=...)`` catalogs."""
    from modules.settings.public import read_module_availability

    return effective_workspace_modules(await read_module_availability(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ))
