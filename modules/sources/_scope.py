"""Source owner scope predicates and admission for the sources owner module."""

from sqlalchemy import ColumnElement
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
from modules.sources.models import Source, SourcePurgeOperation


def _require_source_owner(scope: Scope) -> None:
    """Reject member metadata access; detached scope construction still requires admission."""
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext, InternalJobScope)):
        raise TypeError("An explicit Source scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")


def _source_scope(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Bind SQL to an admitted owner workspace and any exact internal source/generation."""
    _require_source_owner(scope)
    predicates: tuple[ColumnElement[bool], ...] = (Source.workspace_id == scope.workspace_id,)
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates += (Source.id == scope.source_id, Source.generation == scope.source_generation)
    return predicates


def _operation_scope(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    """Bind retained receipt queries without requiring a still-existing canonical Source."""
    _require_source_owner(scope)
    actor_user_id = scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
    predicates: tuple[ColumnElement[bool], ...] = (
        SourcePurgeOperation.workspace_id == scope.workspace_id,
        SourcePurgeOperation.actor_user_id == actor_user_id,
    )
    if isinstance(scope, InternalJobScope) and scope.source_id is not None:
        predicates += (
            SourcePurgeOperation.source_id == scope.source_id,
            SourcePurgeOperation.generation == scope.source_generation,
        )
    return predicates


async def _admit_source_scope(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Check real owner admission; lock auth/workspace/membership before any domain locks.

    The configured instance flag is explicit. No commit, network or fabricated session proof;
    deferred publication callers supply the previously captured fence for configuration CAS.
    A caller already holding domain locks must reuse its admission, never call this with lock.
    """
    _require_source_owner(scope)
    if lock and isinstance(scope, AdminWorkspaceReadContext):
        raise ForbiddenError(detail="Admin workspace access is read-only")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session,
            scope=scope,
            expected=expected,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
