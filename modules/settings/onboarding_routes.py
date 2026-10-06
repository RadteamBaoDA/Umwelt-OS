"""Owner-authenticated API routes for saving and resuming onboarding progress."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.settings.onboarding import read_onboarding_state, save_onboarding_state
from modules.settings.onboarding_schemas import OnboardingStateRead, OnboardingStateUpdate

router = APIRouter(prefix="/api/v1/settings/onboarding", tags=["settings"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("", response_model=OnboardingStateRead)
async def read_progress(session: Session, _owner: OwnerRead) -> OnboardingStateRead:
    """Return the authenticated owner's persisted onboarding step and completion."""
    return await read_onboarding_state(session)


@router.put("", response_model=OnboardingStateRead)
async def save_progress(
    value: OnboardingStateUpdate, session: Session, _owner: OwnerWrite,
) -> OnboardingStateRead:
    """Save one owner-confirmed step with CSRF-backed write authorization and CAS."""
    result = await save_onboarding_state(session, value)
    await session.commit()
    return result
