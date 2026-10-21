"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.erasure_sql import AccountErasureOwner

from . import models as _models

ERASURE_TABLES: tuple[str, ...] = (
    "ai_settings",
    "module_lifecycle_settings",
    "onboarding_state",
    "owner_preferences",
    "retention_settings",
    "translation_settings",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


async def erase_account_settings_references(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Anonymize the departing updater across peer translation settings without deleting them.

    Require original durable deletion authority, including for bootstrap account 1 whose
    disabled identity tombstone prevents FK SET NULL from firing. Only matching updater IDs
    change; peer choices, configuration revisions and update timestamps remain intact.
    This is idempotent and caller-owned transaction work with no commit or external I/O.
    """
    await authorize_account_erasure(session, scope)
    record = _models.TranslationSettingsRecord
    await session.execute(update(record).where(record.updated_by_user_id == scope.account_id).values(
        updated_by_user_id=None, updated_at=record.updated_at,
    ))


async def verify_account_settings_references(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Reject completion while any translation setting still names the departing updater.

    Revalidate original deletion authority and check every workspace without exposing peer
    settings. Caller controls transaction cleanup; this read neither mutates nor commits.
    """
    await authorize_account_erasure(session, scope)
    if await session.scalar(select(1).select_from(_models.TranslationSettingsRecord).where(
        _models.TranslationSettingsRecord.updated_by_user_id == scope.account_id,
    ).limit(1)):
        raise RuntimeError("Account settings updater reference remains")
