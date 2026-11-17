"""Shared accessors for the ARQ worker ``ctx`` mapping and owner-backed job scopes.

Every worker module used to carry private copies of these tiny helpers. Behaviour is identical to
the removed copies; modules that tests monkeypatch keep their private alias (``_factory``,
``_worker_flag``, ``_workspace_job_scope``) bound to these implementations.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.workspaces import public as workspaces
from core.workspaces.schemas import InternalJobScope


def worker_session_factory(ctx: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    """Return the worker-owned async database session factory installed in the ARQ context.

    Pure lookup; raises ``KeyError`` when the worker was started without a session factory.
    """
    return cast(async_sessionmaker[AsyncSession], ctx["session_factory"])


def worker_multi_workspace_flag(ctx: dict[str, object]) -> bool:
    """Return the configured multi-workspace rollout flag from worker settings.

    Raises:
        TypeError: When the setting is not a real ``bool``; no default or inferred rollout is accepted.
    """
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    return enabled


async def resolve_owner_job_scope(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    multi_workspace_enabled: bool,
) -> InternalJobScope | None:
    """Resolve an owner-backed durable job scope for a stored workspace binding.

    Reads the current workspace owner through ``workspaces.resolve_workspace_owner_context`` (no
    lock, no commit) and stamps that owner's membership revision into the scope. Returns ``None``
    when the workspace has no resolvable owner so callers skip the job.
    """
    owner = await workspaces.resolve_workspace_owner_context(
        session,
        workspace_id,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if owner is None:
        return None
    return InternalJobScope(
        workspace_id=workspace_id,
        actor_user_id=owner.user_id,
        membership_revision=owner.membership_revision,
    )
