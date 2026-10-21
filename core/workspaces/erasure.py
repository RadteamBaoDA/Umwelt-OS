"""Deletion-only workspace identity cleanup, separate from normal member admission."""

from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.auth.models import Owner
from core.workspaces.models import (
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceShare,
)


async def erase_account_workspace_references(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Remove departing identity's membership, shares and invitations from peer workspaces.

    The original owned workspace remains until finalization. Deleting an accepted invite keeps
    its non-null acceptance invariant and avoids retaining the departing user or mailbox.
    Caller commits this authorized unit before owner identity deletion.
    """
    await authorize_account_erasure(session, scope)
    owner_email = await session.scalar(select(Owner.email).where(Owner.id == scope.account_id))
    await session.execute(delete(WorkspaceShare).where(WorkspaceShare.member_user_id == scope.account_id))
    await session.execute(delete(WorkspaceMembership).where(
        WorkspaceMembership.user_id == scope.account_id,
        WorkspaceMembership.workspace_id != scope.workspace_id,
    ))
    await session.execute(delete(WorkspaceInvitation).where(
        WorkspaceInvitation.accepted_by_user_id == scope.account_id,
    ))
    if owner_email:
        await session.execute(delete(WorkspaceInvitation).where(WorkspaceInvitation.email == owner_email))


async def finalize_account_workspace_erasure(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Remove an emptied owned workspace after Auth clears its default pointer.

    Authorization is rechecked within the final transaction. Restrict FKs fail closed if any
    domain owner left rows behind, preventing a successful account receipt with retained data.
    """
    await authorize_account_erasure(session, scope)
    await session.execute(delete(WorkspaceShare).where(WorkspaceShare.workspace_id == scope.workspace_id))
    await session.execute(delete(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == scope.workspace_id))
    await session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == scope.workspace_id))
    result = await session.execute(delete(Workspace).where(
        Workspace.id == scope.workspace_id, Workspace.owner_user_id == scope.account_id,
    ))
    if cast(CursorResult[Any], result).rowcount != 1:
        raise RuntimeError("Owned workspace erasure failed")
