"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.erasure_sql import AccountErasureOwner
from modules.translations.models import ContentTranslation, TranslationAdmissionSlot

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "content_translations",
    "translation_admission_slots",
    "translation_batch_items",
    "translation_batches",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


async def erase_account_shared_rows(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Release the singleton translation lease only when its holder belongs to this workspace.

    Incrementing its fencing token invalidates any stale remote completion. The canonical
    translation remains until later SQL deletion, so this ownership proof cannot be lost.
    """
    await authorize_account_erasure(session, scope)
    await session.execute(update(TranslationAdmissionSlot).where(exists(
        select(1).select_from(ContentTranslation).where(
            ContentTranslation.id == TranslationAdmissionSlot.translation_id,
            ContentTranslation.workspace_id == scope.workspace_id,
        )
    )).values(
        translation_id=None, expires_at=None,
        fencing_token=TranslationAdmissionSlot.fencing_token + 1,
    ))


async def verify_account_shared_rows(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Require no singleton slot still points to an owned translation before success."""
    await authorize_account_erasure(session, scope)
    if await session.scalar(select(TranslationAdmissionSlot.id).where(exists(
        select(1).select_from(ContentTranslation).where(
            ContentTranslation.id == TranslationAdmissionSlot.translation_id,
            ContentTranslation.workspace_id == scope.workspace_id,
        )
    )).limit(1)):
        raise RuntimeError("Translation admission slot retains account data")
