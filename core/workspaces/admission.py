"""Canonical actor/admission helpers shared by module public contracts.

About 45 modules carried private ``_actor``/``_admit`` copies. These helpers express every
behaviour-identical variant; modules migrate by calling them with the options that
reproduce their old copy (see ``docs/architecture/workspace-access-helpers.md``). The
fence primitives are resolved through ``core.workspaces.public`` at call time so tests that
patch ``workspaces.read_access_fence``/``lock_access_fence`` keep working.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError
from core.workspaces import public as workspaces
from core.workspaces.schemas import (
    AccessFence,
    AdminWorkspaceReadContext,
    InternalJobScope,
    Scope,
    WorkspaceContext,
)

ALL_SCOPE_TYPES: tuple[type, ...] = (WorkspaceContext, AdminWorkspaceReadContext, InternalJobScope)


def actor_id(scope: Scope) -> int:
    """Return the principal recorded by a workspace request, admin read view, or durable job scope.

    Job scopes yield ``actor_user_id``; request scopes yield ``user_id`` (for the admin read
    view this is the data owner, never the operator). No I/O and no validation of the scope.
    """
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


def require_owner_scope(scope: Scope, *, allowed: tuple[type, ...] = (WorkspaceContext, InternalJobScope),
                        label: str = "workspace") -> None:
    """Reject untyped scopes with ``TypeError`` before any session await.

    Args:
        scope: Candidate scope.
        allowed: Accepted scope classes.
        label: Names the owning domain in the error text ("An explicit <label> scope is required").
    """
    if not isinstance(scope, allowed):
        raise TypeError(f"An explicit {label} scope is required")


def require_workspace_owner(scope: Scope) -> None:
    """Require an explicit owner-capable scope: ``TypeError`` for untyped scopes, 403 for members.

    Accepts ``WorkspaceContext`` (owner role only) and ``InternalJobScope`` (already admitted
    against the stamped owner). Pure; no I/O, so it runs before any session await. This is the
    type check plus member denial that ``admit_scope`` performs, without a fence read.
    """
    require_owner_scope(scope)
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")


async def admit_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
    allowed_scopes: tuple[type, ...] | None = None, scope_label: str = "workspace",
    owner_only: bool = True, admin_read_only_on_lock: bool = False, check_flag: bool = True,
) -> AccessFence:
    """Admit a scope and return its access fence, optionally locking it.

    Check order (matching the majority module copies): scope type (``TypeError``), member
    denial (403 "Workspace owner required"), admin viewer lock denial (403), feature-flag type
    (``TypeError``), then ``lock_access_fence`` (``lock=True``, forwards ``expected``) or a
    non-locking ``read_access_fence``. No commit; the caller owns the transaction and the
    lock ordering rules documented on the fence primitives.

    Args:
        allowed_scopes: When set, scopes of other classes raise ``TypeError`` using ``scope_label``.
        owner_only: Deny ``WorkspaceContext`` members with 403.
        admin_read_only_on_lock: Deny ``AdminWorkspaceReadContext`` when ``lock`` is true.
        check_flag: Require ``multi_workspace_enabled`` to be a real ``bool``.
    """
    if allowed_scopes is not None:
        require_owner_scope(scope, allowed=allowed_scopes, label=scope_label)
    if owner_only and isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")
    if admin_read_only_on_lock and lock and isinstance(scope, AdminWorkspaceReadContext):
        raise ForbiddenError(detail="Admin workspace access is read-only")
    if check_flag and type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
