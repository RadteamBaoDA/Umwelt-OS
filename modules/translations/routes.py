"""Translation batch endpoints: members may request/read shared resources; no owner role needed."""

import contextlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account_write
from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.workspaces.dependencies import require_workspace_read
from core.workspaces.schemas import WorkspaceContext
from modules.translations import (
    inputs as _inputs,  # noqa: F401  # import registers the resource authorizers
)
from modules.translations import public
from modules.translations.schemas import (
    TranslationBatchAccepted,
    TranslationBatchRead,
    TranslationBatchRequest,
)

router = APIRouter(prefix="/api/v1/translations", tags=["translations"])
Session = Annotated[AsyncSession, Depends(get_session)]
Member = Annotated[WorkspaceContext, Depends(require_workspace_read)]
# CSRF + backup admission without the owner-role requirement of require_workspace_write.
WriteAdmission = Annotated[object, Depends(require_account_write)]


@router.post("/batches", response_model=TranslationBatchAccepted, status_code=202)
async def create_batch(
    value: TranslationBatchRequest, request: Request, session: Session,
    member: Member, _admission: WriteAdmission,
) -> TranslationBatchAccepted:
    """Authorize and queue references; disabled workspaces get blocked items and nothing is queued."""
    result = await public.request_translations(
        session, member, value,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),),
        app_settings=request.app.state.settings, redis=request.app.state.redis)
    await session.commit()
    for translation_id in result.enqueue_ids:  # best effort: recover_translation_jobs covers a lost enqueue
        with contextlib.suppress(Exception):
            await request.app.state.redis.enqueue_job(
                "translate_content", str(translation_id), _job_id=f"translation:{translation_id}")
    return result


@router.get("/batches/{batch_id}", response_model=TranslationBatchRead)
async def read_batch(batch_id: UUID, request: Request, session: Session, member: Member) -> TranslationBatchRead:
    """Read this actor's batch in this workspace; other actors and workspaces see 404."""
    return await public.read_batch(
        session, batch_id, scope=member,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
