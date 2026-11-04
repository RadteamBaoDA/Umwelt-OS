"""Workspace admission and scope predicates shared by every Documents operation."""

from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    select,
)
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
from modules.knowledge.documents.models import (
    Document,
)
from modules.sources import public as sources


def _require_document_owner(scope: Scope) -> None:
    """Require an explicit owner/internal subject; membership never grants shared content.

    Construction is not admission. Public callers must additionally revalidate the actual
    actor, membership and feature gate through the workspace owner before any query.
    """
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext, InternalJobScope)):
        raise TypeError("An explicit Document scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")


def _document_scope(scope: Scope, *, allow_member: bool = False) -> tuple[ColumnElement[bool], ...]:
    """Constrain roots before paging to admitted workspace and Source-owned retained lineage.

    Paused and connector-only archived Sources remain readable. Queued/running/failed
    data purges hide their content; the Source projection grants no membership/resource
    authority and callers must first perform real owner admission. No Source ORM is read.
    Only member-read callers pass ``allow_member``; a member then sees just active-grant
    Documents (grant subquery is part of the predicate, so it precedes LIMIT/count/cursor).
    """
    member = allow_member and isinstance(scope, WorkspaceContext) and scope.role != "owner"
    if not member:
        _require_document_owner(scope)
    predicates: tuple[ColumnElement[bool], ...] = (
        Document.workspace_id == scope.workspace_id,
        Document.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
    )
    if member:
        assert isinstance(scope, WorkspaceContext)
        predicates += (Document.id.in_(workspaces.granted_resource_ids(scope=scope, kind="document")),)
    return predicates


async def _admit_document_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool, allow_member: bool = False,
) -> AccessFence:
    """Revalidate actual owner/default membership with the explicit rollout gate, without locks.

    Members are denied before root IDs, counts or content. This snapshot is not HTTP-session
    or final-send proof; writers acquire their early Source/access set and routes retain
    exact authenticated-session locks through commit. No commit or external I/O occurs.
    """
    if not (allow_member and isinstance(scope, WorkspaceContext)):
        _require_document_owner(scope)
    return await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def _read_document_source_id(
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Locate one visible root's Source before acquiring any domain lock, without metadata.

    Scope/Source lineage precedes the exact ID query. Missing, foreign and data-purged
    documents return None alike; writers then lock Source and freshly reload their own root.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(select(Document.source_id).where(Document.id == document_id, *_document_scope(scope)))
