"""Deletion-only authority and safe receipt projection; never normal account admission."""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_models import AccountDeletionOperation
from core.auth.deletion_schemas import DeletionStatus
from core.auth.erasure_contracts import AccountErasureScope
from core.auth.models import Owner
from core.workspaces.public import account_deletion_blockers


async def authorize_account_erasure(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Require durable original scope and disabled identity before owner cleanup writes.

    Construction of a scope grants nothing. Failed operations remain retryable; success,
    missing/mismatched identities, and a foreign workspace fail closed. The workspace owner
    check survives clearing the account's default pointer during finalization. No commit or lock.
    """
    operation = await session.scalar(select(AccountDeletionOperation).where(
        AccountDeletionOperation.id == scope.operation_id,
        AccountDeletionOperation.account_id == scope.account_id,
        AccountDeletionOperation.workspace_id == scope.workspace_id,
        AccountDeletionOperation.status.in_(("queued", "running", "failed")),
    ).execution_options(populate_existing=True))
    owner = await session.scalar(select(Owner).where(Owner.id == scope.account_id).execution_options(populate_existing=True))
    if operation is None or owner is None or owner.account_state != "disabled":
        raise PermissionError("Account erasure scope is not authorized")
    workspace_id, _ = await account_deletion_blockers(session, scope.account_id)
    if workspace_id != scope.workspace_id:
        raise PermissionError("Account erasure scope is not authorized")


async def receipt_operation(session: AsyncSession, digest: str) -> AccountDeletionOperation | None:
    """Find an unexpired receipt without revealing whether any account exists."""
    row = await session.scalar(select(AccountDeletionOperation).where(AccountDeletionOperation.receipt_hash == digest))
    if row is not None and row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        return None
    return row


def deletion_status(operation: AccountDeletionOperation) -> DeletionStatus:
    """Project durable progress without raw identifiers, internal errors or content."""
    return DeletionStatus(status=operation.status, phase=operation.phase, error_code=operation.error_code)
