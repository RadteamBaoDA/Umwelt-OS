"""Owner-scoped public persistence contract for resumable onboarding progress."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from modules.settings.models import OnboardingStateRecord
from modules.settings.onboarding_schemas import OnboardingStateRead, OnboardingStateUpdate
from modules.settings.public import require_preferences_account
from core.auth.schemas import AccountSessionRef


def _read(row: OnboardingStateRecord | None) -> OnboardingStateRead:
    """Project persisted progress without deriving completion from browser state."""
    return OnboardingStateRead(
        configuration_revision=row.configuration_revision if row else 1,
        current_step=row.current_step if row else "ai_privacy",
        data_choice=row.data_choice if row else None,
        completed_at=row.completed_at if row else None,
    )


async def read_onboarding_state(session: AsyncSession, *, actor_user_id: int, multi_workspace_enabled: bool) -> OnboardingStateRead:
    """Read only active actor progress without selected workspace; absent row means not started."""
    await require_preferences_account(session, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(
        select(OnboardingStateRecord)
        .where(OnboardingStateRecord.owner_id == actor_user_id)
        .execution_options(populate_existing=True)
    )
    return _read(row)


async def save_onboarding_state(
    session: AsyncSession, update: OnboardingStateUpdate,
    *, actor_user_id: int, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> OnboardingStateRead:
    """Persist actor progress after auth locks with CAS; caller completes transaction.

    Account preferences never borrow bootstrap onboarding, selected workspace or translation
    settings. Existing ordered steps/data-choice/completion invariants remain unchanged.
    """
    await require_preferences_account(session, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled,
                                      locked=True, auth_sessions=auth_sessions)
    await session.execute(
        insert(OnboardingStateRecord)
        .values(owner_id=actor_user_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(
        select(OnboardingStateRecord)
        .where(OnboardingStateRecord.owner_id == actor_user_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        raise RuntimeError("Onboarding state singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Onboarding progress changed; reload before saving")
    if row.completed_at is not None and update.current_step != "complete":
        raise HTTPException(status_code=409, detail="Completed onboarding cannot be reopened")
    order = ("ai_privacy", "capability", "sources", "sample_or_import", "indexing", "complete")
    if order.index(update.current_step) > order.index(row.current_step) + 1:
        raise HTTPException(status_code=409, detail="Complete onboarding steps in order")
    if update.data_choice is not None and update.current_step == "sample_or_import":
        row.data_choice = update.data_choice
    elif update.data_choice is not None and update.data_choice != row.data_choice:
        raise HTTPException(status_code=409, detail="Choose sample data or personal import on its onboarding step")
    if update.current_step == "indexing" and row.current_step == "sample_or_import" and row.data_choice is None:
        raise HTTPException(status_code=409, detail="Choose sample data or personal import before indexing")
    if update.current_step == "complete" and (
        row.current_step != "indexing" or row.data_choice is None
    ):
        raise HTTPException(status_code=409, detail="Complete the earlier onboarding steps before finishing")
    row.current_step = update.current_step
    if update.current_step == "complete" and row.completed_at is None:
        row.completed_at = datetime.now(UTC)
    row.configuration_revision += 1
    await session.flush()
    return _read(row)
