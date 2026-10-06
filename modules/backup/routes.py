"""Owner-authenticated backup control and operation status routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_backup_owner_write, require_owner
from core.auth.models import AuthSession
from core.database import get_session
from modules.backup import public
from modules.backup.schemas import BackupControlRead, BackupOperationRead

router = APIRouter(prefix="/api/v1/backups", tags=["backups"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_backup_owner_write)]


@router.get("/control", response_model=BackupControlRead)
async def read_backup_control(session: Session, _owner: OwnerRead) -> BackupControlRead:
    """Return pure maintenance status while never creating or rotating owner state."""
    return await public.read_control(session)


@router.post("/operations", response_model=BackupOperationRead, status_code=202)
async def request_backup_operation(
    session: Session, _owner: OwnerWrite, response: Response,
) -> BackupOperationRead:
    """Persist one pending backup intent without claiming that a host runner is available."""
    response.headers["Cache-Control"] = "private, no-store"
    operation = await public.create_operation_intent(session)
    await session.commit()
    return operation


@router.get("/operations/{operation_id}", response_model=BackupOperationRead)
async def read_backup_operation(
    operation_id: UUID, session: Session, _owner: OwnerRead,
) -> BackupOperationRead:
    """Read one credential-free owner operation projection."""
    return await public.read_operation(session, operation_id)
