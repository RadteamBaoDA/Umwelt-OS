"""Admission helper shared by the private observation public-contract submodules."""

# isort: split
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, Scope


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
) -> AccessFence:
    """Admit owner-only observation access before domain reads or writes; W3 owns member grants.

    Delegates to the canonical ``admit_scope`` (owner-only, no scope-type or admin-lock restriction, flag
    must be a real ``bool``); ``lock=True`` locks the access fence. No commit.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
    )
