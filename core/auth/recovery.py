"""Host-only recovery of the sanitized bootstrap tombstone; never an HTTP admission path."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Integer, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from core.auth.public import lock_account_lifecycle
from core.auth.service import hash_password
from core.database import Base
from core.workspaces.public import provision_default_workspace_in_uow


async def recover_bootstrap_account_in_uow(session: AsyncSession, password: str) -> None:
    """Stage a fresh private workspace/password for an erased account 1, without a session.

    Host access to the target database is the authority: this function must never be exposed
    through public setup or an API. Require the exact finalizer tombstone and absence of all
    registered personal references, including incomplete deletion operations. Auth locks the
    account before workspace creation; the caller commits or rolls back the complete unit.
    No old workspace, grants, content, provider credentials or browser sessions are restored.
    ValueError rejects invalid passwords, incomplete cleanup and any non-tombstone identity.
    """
    # Import the owner registry to register the complete schema before the read projection.
    from core.auth import deletion_cleanup as _deletion_cleanup  # noqa: F401

    if not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12 to 128 characters")
    password_hash = await asyncio.to_thread(hash_password, password)
    await lock_account_lifecycle(session, (1,))
    owner = await session.get(Owner, 1, populate_existing=True)
    if (owner is None or owner.account_state != "disabled" or owner.password_hash != ""
            or owner.email is not None or owner.default_workspace_id is not None
            or owner.email_verified_at is not None or owner.email_verification_source is not None
            or owner.created_at != datetime(1970, 1, 1, tzinfo=UTC)):
        raise ValueError("Recovery requires the sanitized, fully erased bootstrap tombstone")

    # Read-only recovery projection: actual owner FKs and identity columns only. A remaining
    # operation/workspace/domain reference must fail closed rather than attach old data again.
    references = []
    for table in Base.metadata.tables.values():
        if table.name == "owner":
            continue
        columns = [column for column in table.c if isinstance(column.type, Integer) and (
            column.name in {"owner_id", "account_id", "user_id"}
            or column.name.endswith("_user_id")
            or any(foreign.target_fullname == "owner.id" for foreign in column.foreign_keys)
        )]
        if columns:
            references.append(exists(select(1).select_from(table).where(or_(
                *(column == 1 for column in columns),
            ))))
    if not references or await session.scalar(select(or_(*references))):
        raise ValueError("Bootstrap cleanup is incomplete; recovery refused")
    owner.default_workspace_id = await provision_default_workspace_in_uow(session, owner.id)
    owner.password_hash = password_hash
    owner.created_at = datetime.now(UTC)
    owner.account_state = "active"
    await session.flush()
