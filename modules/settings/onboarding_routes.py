"""Owner-authenticated API routes for saving and resuming onboarding progress."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.public import authenticated_session_ref
from core.database import get_session
from modules.settings.onboarding import read_onboarding_state, save_onboarding_state
from modules.settings.onboarding_schemas import OnboardingStateRead, OnboardingStateUpdate

router = APIRouter(prefix="/api/v1/settings/onboarding", tags=["settings"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[object, Depends(require_account)]
OwnerWrite = Annotated[object, Depends(require_account_write)]


@router.get("", response_model=OnboardingStateRead)
async def read_progress(request: Request, session: Session, _owner: OwnerRead) -> OnboardingStateRead:
    """Return actual active account onboarding; selected workspace is irrelevant."""
    return await read_onboarding_state(session, actor_user_id=request.state.account.id,
                                       multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.put("", response_model=OnboardingStateRead)
async def save_progress(
    value: OnboardingStateUpdate, request: Request, session: Session, _owner: OwnerWrite,
) -> OnboardingStateRead:
    """Save account step with exact session/CSRF/backup admission, auth-before-state lock and CAS."""
    result = await save_onboarding_state(session, value, actor_user_id=request.state.account.id,
                                         multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
                                         auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return result
