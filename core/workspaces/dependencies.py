"""Explicit HTTP workspace preparation; identity admission is separate from resource access."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.public import get_active_account
from core.auth.schemas import AccountRead
from core.database import get_session
from core.workspaces.public import read_access_fence, resolve_workspace_context
from core.workspaces.schemas import WorkspaceContext


def _parse_workspace_id(workspace_id: UUID | str | None) -> UUID | None:
    """Parse explicit selection here so malformed headers return the contractual 400."""
    if workspace_id is None or isinstance(workspace_id, UUID):
        return workspace_id
    try:
        return UUID(workspace_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid_workspace_id") from None


async def _request_account(request: Request, session: AsyncSession) -> AccountRead:
    """Recheck the authenticated detached account with this app's explicit rollout gate.

    Never read Auth/Session ORM or infer activation from role/ID. Auth dependencies retain
    their exact session, CSRF and backup admission behavior; this recheck grants no resource
    authority and must be repeated under auth-owned session locks for W4 publication.
    """
    authenticated = getattr(request.state, "account", None)
    if not isinstance(authenticated, AccountRead):
        raise HTTPException(status_code=401, detail="Authentication required")
    current = await get_active_account(
        session, authenticated.id,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if current is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    request.state.account = current
    return current


async def _selected_workspace(
    request: Request, session: AsyncSession, account_id: int, workspace_id: UUID | str | None,
) -> WorkspaceContext:
    """Resolve current visible membership; only bootstrap can omit proven default selection.

    Missing nonbootstrap selection and malformed UUID are 400, invalid actor 401, invisible
    workspace 404. No locks/commits: prepare only, with no member resource visibility.
    """
    account = await _request_account(request, session)
    if account_id != account.id:
        raise HTTPException(status_code=401, detail="Authentication required")
    selected = _parse_workspace_id(workspace_id)
    if selected is None:
        if account.id != 1:
            raise HTTPException(status_code=400, detail="workspace_required")
        selected = account.default_workspace_id
    context = await resolve_workspace_context(session, account.id, selected)
    if context is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await read_access_fence(
        session, scope=context, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    return context


async def _default_workspace(
    request: Request, session: AsyncSession, workspace_id: UUID | str | None,
) -> WorkspaceContext:
    """Derive actor-owned default; reject explicit Chat/Memory context mismatch with 409.

    Unlike ordinary selection, private routes derive every actor's default from the current
    auth DTO. An invited or arbitrary header never moves private activity or its retrieval.
    Missing/inactive/default-owner lineage fails closed; this is preparation without locks.
    """
    account = await _request_account(request, session)
    selected = _parse_workspace_id(workspace_id)
    if selected is not None and selected != account.default_workspace_id:
        raise HTTPException(status_code=409, detail="default_workspace_required")
    context = await resolve_workspace_context(session, account.id, account.default_workspace_id)
    if context is None or context.role != "owner":
        raise HTTPException(status_code=404, detail="Workspace not found")
    await read_access_fence(
        session, scope=context, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    return context


async def require_workspace_read(
    request: Request,
    auth_session: Annotated[object, Depends(require_account)],
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> WorkspaceContext:
    """Authenticate and prepare selected identity; members still need explicit resource grants.

    Auth dependency owns its session persistence; use only its detached request account.
    Publication must later supply authenticated_session_ref to the locked access fence.
    """
    account = getattr(request.state, "account", None)
    if not isinstance(account, AccountRead):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await _selected_workspace(request, session, account.id, workspace_id)


async def require_workspace_write(
    request: Request,
    auth_session: Annotated[object, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> WorkspaceContext:
    """Require existing CSRF/backup-admitted account write and selected owner membership.

    No write or lifecycle lock occurs here. Owner handlers retain their auth lifecycle order,
    CAS, domain authorization and caller-owned transaction before mutations/publication.
    """
    account = getattr(request.state, "account", None)
    if not isinstance(account, AccountRead):
        raise HTTPException(status_code=401, detail="Authentication required")
    context = await _selected_workspace(request, session, account.id, workspace_id)
    if context.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return context


async def require_default_workspace_read(
    request: Request,
    auth_session: Annotated[object, Depends(require_account)],
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> WorkspaceContext:
    """Prepare private Chat/Memory under the active actor's owned default workspace only.

    An explicit different selection returns 409 default_workspace_required. Domain readers
    still bind each private resource to this actor/default and fence later publication.
    """
    return await _default_workspace(request, session, workspace_id)


async def require_default_workspace_write(
    request: Request,
    auth_session: Annotated[object, Depends(require_account_write)],
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> WorkspaceContext:
    """Retain CSRF/backup admission and require actor-owned default for private mutations.

    Auth dependency commits its existing admission before endpoint locks. This preparation
    commits nothing; caller applies locked fence, domain checks and any required CAS.
    """
    context = await _default_workspace(request, session, workspace_id)
    if context.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return context
