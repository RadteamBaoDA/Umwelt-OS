import asyncio
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner
from core.auth.models import AuthSession
from core.auth.routes import get_auth_redis
from core.database import get_session
from core.system.health import system_health
from modules.sources.public import read_source_purge_operation
from modules.sources.schemas import OperationRead

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# Readiness covers pool checkout under load; the compose healthcheck allows 5 s, so stay below it.
READY_TIMEOUT_SECONDS = 3.0


@router.get("/health")
async def read_system_health(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
    redis: Annotated[Any, Depends(get_auth_redis)],
) -> dict[str, Any]:
    """Return authenticated component health using the current application settings."""
    return await system_health(session, redis, request.app.state.settings)


@router.get("/operations/{operation_id}", response_model=OperationRead)
async def get_operation(
    operation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _owner: Annotated[AuthSession, Depends(require_owner)],
) -> OperationRead:
    """Read one source purge operation and return its public status fields or 404."""
    # PRODUCTION FIX: the inline OperationRead(...) omitted required stage fields (ValidationError -> 500).
    operation = await read_source_purge_operation(session, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


@router.get("/ready", include_in_schema=False)
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Return readiness only when a bounded database probe succeeds."""
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), READY_TIMEOUT_SECONDS)
    except (SQLAlchemyError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Database is not ready") from exc
    return {"status": "ready"}
