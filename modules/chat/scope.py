"""Minimal W2 call-shape shims: resolve Chat's owner-default scope for Memory/Agents calls.

Full workspace conversion of Chat is slice A2; this only supplies the new required arguments.
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.workspaces.schemas import WorkspaceContext


def multi_workspace_enabled() -> bool:
    """Read the rollout gate (Chat helpers have no request/settings handle)."""
    return Settings().multi_workspace_enabled


async def owner_default_scope(session: AsyncSession, owner_id: int = 1) -> WorkspaceContext:
    """Resolve the owner's default-workspace context; 404 when the account has none."""
    from core.auth.public import get_active_account
    from core.workspaces.public import resolve_workspace_context

    account = await get_active_account(session, owner_id, multi_workspace_enabled=multi_workspace_enabled())
    context = (
        await resolve_workspace_context(session, account.id, account.default_workspace_id)
        if account is not None else None
    )
    if context is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return context


async def read_owner_export_privacy(session: AsyncSession, owner_id: int = 1):  # type: ignore[no-untyped-def]
    """`read_export_privacy` under the owner's default scope."""
    from modules.memory.public import read_export_privacy

    return await read_export_privacy(
        session, scope=await owner_default_scope(session, owner_id),
        multi_workspace_enabled=multi_workspace_enabled(),
    )
