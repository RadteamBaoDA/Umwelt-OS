"""Short admission fences and lifecycle locks; callers own transaction completion.

Runtime admission discovers IDs before auth-owned account/session locks. Existing W1b
management helpers still require their caller's exclusive auth lifecycle locks first.
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.schemas import AccountRead, AccountSessionRef
from core.workspaces.models import Workspace, WorkspaceMembership
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext


def require_revision(expected_revision: int | None, actual_revision: int) -> None:
    """Require administration CAS; missing 428 and stale visible revision 409."""
    if expected_revision is None:
        raise HTTPException(status_code=428, detail="Workspace revision is required")
    if expected_revision != actual_revision:
        raise HTTPException(status_code=409, detail="Workspace revision changed")


async def lock_workspace_memberships(
    session: AsyncSession, workspace_id: UUID, user_ids: tuple[int, ...],
) -> tuple[Workspace, dict[int, WorkspaceMembership]]:
    """Lock existing workspace then sorted membership keys and reread their current values.

    Caller first holds all required auth account/session locks. No commit or external I/O;
    absent workspace returns 404. Missing membership must be interpreted by the caller.
    """
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    rows = await session.scalars(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id.in_(user_ids),
        ).order_by(WorkspaceMembership.user_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    return workspace, {row.user_id: row for row in rows}


async def lock_owner_management(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int, *,
    expected_revision: int | None, target_user_id: int | None = None,
) -> tuple[Workspace, dict[int, WorkspaceMembership]]:
    """Require current owner membership before checking visible CAS under lifecycle locks.

    Invisible/missing membership is 404, member management is 403, missing revision 428,
    stale visible revision 409. Target membership keys are sorted with actor membership.
    """
    ids = (actor_user_id,) if target_user_id is None else (actor_user_id, target_user_id)
    workspace, memberships = await lock_workspace_memberships(session, workspace_id, ids)
    actor = memberships.get(actor_user_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if actor.role != "owner" or workspace.owner_user_id != actor_user_id or actor.owner_user_id != actor_user_id:
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if target_user_id is not None and target_user_id not in memberships:
        raise HTTPException(status_code=404, detail="Member not found")
    require_revision(expected_revision, workspace.configuration_revision)
    return workspace, memberships


async def lock_share_management(
    session: AsyncSession, workspace_id: UUID, actor_user_id: int, target_user_id: int, expected_revision: int | None,
) -> tuple[Workspace, dict[int, WorkspaceMembership]]:
    """Owner-only share lock; CAS is the target member's membership revision (not the workspace's).

    Order: actor invisible 404, non-owner 403, target absent or the owner 404, missing CAS 428,
    stale CAS 409. Caller holds auth locks first; share row and resource locks follow.
    """
    ids = tuple(sorted({actor_user_id, target_user_id}))
    workspace, memberships = await lock_workspace_memberships(session, workspace_id, ids)
    actor = memberships.get(actor_user_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if actor.role != "owner" or workspace.owner_user_id != actor_user_id or actor.owner_user_id != actor_user_id:
        raise HTTPException(status_code=403, detail="Workspace owner required")
    target = memberships.get(target_user_id)
    if target is None or target_user_id == actor_user_id or target.role != "member":
        raise HTTPException(status_code=404, detail="Member not found")
    require_revision(expected_revision, target.revision)
    return workspace, memberships


def _scope_actor(scope: Scope) -> int:
    """Accept only a detached typed subject; queued/client dictionaries are never trusted."""
    if isinstance(scope, InternalJobScope):
        return scope.actor_user_id
    if isinstance(scope, WorkspaceContext):
        return scope.user_id
    raise HTTPException(status_code=401, detail="Authentication required")


def _check_access(
    workspace: Workspace, memberships: dict[int, WorkspaceMembership], accounts: dict[int, AccountRead | None],
    *, scope: Scope, actor_user_id: int, discovered_owner_id: int, expected: AccessFence | None,
) -> AccessFence:
    """Check current owner/membership visibility before returning or comparing a visible fence.

    Account identity failure is 401. Missing/revoked/invalid owner lineage is invisible 404;
    current visible membership role/revision or expected config disagreement is 409. Member
    admission never implies resource visibility. Source/grant/privacy/claim checks are owned
    by their domain modules and must follow this fence under the same short transaction.
    """
    actor_account = accounts.get(actor_user_id)
    if actor_account is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    owner_account = accounts.get(discovered_owner_id)
    owner_membership = memberships.get(discovered_owner_id)
    actor_membership = memberships.get(actor_user_id)
    if (workspace.owner_user_id != discovered_owner_id or not workspace.is_default
            or owner_account is None or owner_account.default_workspace_id != workspace.id
            or owner_membership is None or owner_membership.role != "owner"
            or owner_membership.owner_user_id != discovered_owner_id
            or owner_membership.revision <= 0 or actor_membership is None):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if (actor_membership.role not in {"owner", "member"} or actor_membership.revision <= 0
            or (actor_membership.role == "owner" and (
                actor_user_id != discovered_owner_id or actor_membership.owner_user_id != actor_user_id
            )) or (actor_membership.role == "member" and actor_membership.owner_user_id is not None)):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if isinstance(scope, InternalJobScope):
        if (actor_membership.role != "owner" or actor_user_id != discovered_owner_id
                or actor_account.default_workspace_id != workspace.id):
            raise HTTPException(status_code=404, detail="Workspace not found")
    elif actor_membership.role != scope.role:
        raise HTTPException(status_code=409, detail="Workspace membership changed")
    if actor_membership.revision != scope.membership_revision:
        raise HTTPException(status_code=409, detail="Workspace membership changed")
    fence = AccessFence(
        workspace_id=workspace.id, user_id=actor_user_id,
        membership_revision=actor_membership.revision,
        configuration_revision=workspace.configuration_revision,
    )
    if expected is not None and fence != expected:
        raise HTTPException(status_code=409, detail="Workspace access fence changed")
    return fence


async def _prepare_access(
    session: AsyncSession, *, scope: Scope, locked: bool, expected: AccessFence | None,
    multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> AccessFence:
    """Discover IDs without locks, then reread auth->workspace->sorted memberships.

    This must start before any domain/later lock; discovering extra identities later requires
    a transaction restart. Locked admission owns no commits, external I/O or browser bearer.
    Nonlocking preparation is a snapshot only and must be reacquired for publication.
    """
    from core.auth.public import get_active_account, lock_account_admission

    actor_user_id = _scope_actor(scope)
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("Explicit boolean multi-workspace gate is required")
    if expected is not None and not isinstance(expected, AccessFence):
        raise ValueError("A detached access fence is required")
    # Only IDs are discovered here. No earlier auth row may be discovered and locked
    # opportunistically after a workspace/resource lock has been acquired.
    discovered_owner_id = await session.scalar(
        select(Workspace.owner_user_id).where(Workspace.id == scope.workspace_id)
    )
    if locked:
        ids = (actor_user_id,) if discovered_owner_id is None else (actor_user_id, discovered_owner_id)
        accounts = await lock_account_admission(
            session, ids, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled,
            auth_sessions=auth_sessions,
        )
    else:
        actor = await get_active_account(
            session, actor_user_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if actor is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        accounts = {actor_user_id: actor}
        if discovered_owner_id is not None and discovered_owner_id != actor_user_id:
            accounts[discovered_owner_id] = await get_active_account(
                session, discovered_owner_id, multi_workspace_enabled=multi_workspace_enabled,
            )
    if discovered_owner_id is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ids = tuple(sorted({actor_user_id, discovered_owner_id}))
    if locked:
        workspace, memberships = await lock_workspace_memberships(session, scope.workspace_id, ids)
    else:
        workspace = await session.scalar(
            select(Workspace).where(Workspace.id == scope.workspace_id)
            .execution_options(populate_existing=True)
        )
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        memberships = {row.user_id: row for row in await session.scalars(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == scope.workspace_id,
                WorkspaceMembership.user_id.in_(ids),
            ).order_by(WorkspaceMembership.user_id).execution_options(populate_existing=True)
        )}
    return _check_access(
        workspace, memberships, accounts, scope=scope, actor_user_id=actor_user_id,
        discovered_owner_id=discovered_owner_id, expected=expected,
    )


async def read_access_fence(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool = False,
) -> AccessFence:
    """Prepare a current active-account/owner/membership/config snapshot without row locks.

    Pass the actual configured feature gate explicitly for requests and detached workers.
    Typed scope is a claimed subject, never resource authority or exact logout proof. Reads
    current values with populate_existing; 401 invalid actor, 404 invisible, 409 stale scope.
    Caller must lock/recheck this snapshot and domain-specific fences before publication.
    """
    return await _prepare_access(
        session, scope=scope, locked=False, expected=None, multi_workspace_enabled=multi_workspace_enabled,
    )


async def lock_access_fence(
    session: AsyncSession, *, scope: Scope, expected: AccessFence | None = None,
    multi_workspace_enabled: bool = False, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> AccessFence:
    """Lock/revalidate account(+exact sessions)->workspace->membership in a short transaction.

    Call before any Source/resource lock. Auth owns all account/session persistence; its
    sorted FOR SHARE locks conflict with disable/logout. Workspace and sorted membership
    rows use FOR UPDATE with current reloads. No commit or external I/O: caller releases
    before network and reacquires expected fence plus source/grant/privacy/claim checks.

    HTTP publication must supply its authenticated actor's exact AccountSessionRef through
    auth_sessions; the empty tuple is worker/account admission only and cannot prove logout.
    W4 separately owns actual bounded ASGI sends and final transaction cleanup. Gate is the
    explicit configured value, default false. 401 invalid identity/session; 404 invisible or
    revoked membership; 409 stale visible role/membership/configuration fence.
    """
    return await _prepare_access(
        session, scope=scope, locked=True, expected=expected,
        multi_workspace_enabled=multi_workspace_enabled, auth_sessions=auth_sessions,
    )


async def authorize_internal_job(
    session: AsyncSession, *, scope: InternalJobScope, multi_workspace_enabled: bool = False,
) -> AccessFence:
    """Admit a durable worker subject only as its active owned-default workspace actor.

    Caller first compares all scope/claim values to its durable job/receipt, then calls this
    before Source/resource locks. No selected/member/browser scope or account1 fallback.
    Paired source restrictions require Source-owner generation/tombstone checks afterward.
    Returns a locked fence without committing; release before network, recheck to publish.
    """
    if not isinstance(scope, InternalJobScope):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await lock_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


def can_read_resource(scope: WorkspaceContext, resource_workspace_id: UUID, shared_to_actor: bool) -> bool:
    """Pure predicate, no DB access: same workspace and (owner or an active share for the actor)."""
    return scope.workspace_id == resource_workspace_id and (scope.role == "owner" or shared_to_actor)


async def assert_resource_access(
    session: AsyncSession, scope: WorkspaceContext, kind: str, resource_id: UUID, expected_revision: int,
) -> None:
    """Owner passes; a member needs a live grant at ``expected_revision`` (FOR SHARE locked), else 404."""
    if scope.role == "owner":
        return
    from core.workspaces.public import lock_resource_grants, read_resource_grants

    grants = await read_resource_grants(session, scope=scope, kind=kind, resource_ids=(resource_id,))  # type: ignore[arg-type]
    if not grants or grants[0].resource_revision != expected_revision:
        raise HTTPException(status_code=404, detail="Resource not found")
    await lock_resource_grants(session, scope=scope, grants=grants)
