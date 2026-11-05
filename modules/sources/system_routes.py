"""Sources-owned system operation route, mounted under ``/api/v1/system`` by the Sources descriptor.

The route lives with the module that owns the purge DTO so ``core`` never imports ``modules``; its URL, auth and
response are unchanged from when it was declared in ``core.system.routes``.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.database import get_session
from core.errors import DomainError
from modules.sources.public import (
    OperationRead,
    read_source_purge_operation,
    resolve_source_purge_job_scope,
)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/operations/{operation_id}", response_model=OperationRead)
async def get_operation(
    request: Request,
    operation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> OperationRead:
    """Read one source purge operation by its retained job scope and return its public status fields or 404."""
    # PRODUCTION FIX: the inline OperationRead(...) omitted required stage fields (ValidationError -> 500).
    enabled = request.app.state.settings.multi_workspace_enabled
    try:
        scope = await resolve_source_purge_job_scope(session, operation_id, multi_workspace_enabled=enabled)
        operation = (
            None if scope is None
            else await read_source_purge_operation(session, operation_id, scope=scope, multi_workspace_enabled=enabled)
        )
    except (HTTPException, DomainError) as exc:
        if exc.status_code in {401, 403, 409}:  # the job's actor lost access: indistinguishable from unknown
            raise HTTPException(status_code=404, detail="Operation not found") from exc
        raise
    finally:
        await session.rollback()  # the resolver takes ordered auth/workspace locks; release them
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation
