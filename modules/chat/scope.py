"""Minimal W2 call-shape shims: resolve Chat's owner-default scope for Memory/Agents calls.

Full workspace conversion of Chat is slice A2; this only supplies the new required arguments.
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.model_gateway.client import PrivacyPolicyDenied
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


async def owner_scope_kwargs(session: AsyncSession, owner_id: int = 1) -> dict[str, object]:
    """`scope`/`multi_workspace_enabled` keyword arguments for owner-backed Chat call sites."""
    return {"scope": await owner_default_scope(session, owner_id), "multi_workspace_enabled": multi_workspace_enabled()}


async def read_owner_export_privacy(session: AsyncSession, owner_id: int = 1):  # type: ignore[no-untyped-def]
    """`read_export_privacy` under the owner's default scope."""
    from modules.memory.public import read_export_privacy

    return await read_export_privacy(
        session, scope=await owner_default_scope(session, owner_id),
        multi_workspace_enabled=multi_workspace_enabled(),
    )


async def ensure_ai_config_unchanged(session_factory, settings, redis, scope, snapshot, alias, mapping) -> None:  # type: ignore[no-untyped-def]
    """Fresh-session recheck at ``before_send``: raise when settings/access drifted since the snapshot."""
    from modules.settings import public as settings_public

    async with session_factory() as check:
        current = await settings_public.get_ai_execution_config(check, settings, redis, scope=scope)
    if (
        current.configuration_revision != snapshot.configuration_revision
        or current.gateway_identity != snapshot.gateway_identity
        or current.endpoint_destination_id != snapshot.endpoint_destination_id
        or current.aliases.get(alias) != mapping
    ):
        raise PrivacyPolicyDenied("Chat model configuration changed before send")
