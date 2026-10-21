"""Durable account erasure recovery, independent of login or best-effort queue delivery."""
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.auth.deletion_models import AccountDeletionOperation
from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.auth.models import AuthSession, GoogleIdentity, Owner


async def finalize_account_erasure(session: AsyncSession, operation: AccountDeletionOperation) -> None:
    """Remove identity only after all required cleanup; retain disabled bootstrap tombstone.

    Caller owns the per-operation advisory lock and transaction. Clear the composite default
    pointer before workspace deletion; completed receipt drops all personal scope/cursor fields.
    """
    from core.workspaces.erasure import finalize_account_workspace_erasure

    assert operation.account_id is not None and operation.workspace_id is not None
    scope = AccountErasureScope(operation.id, operation.account_id, operation.workspace_id)
    await authorize_account_erasure(session, scope)
    owner = await session.get(Owner, scope.account_id)
    assert owner is not None
    owner.default_workspace_id = None
    await session.flush()
    await finalize_account_workspace_erasure(session, scope)
    await session.execute(delete(AuthSession).where(AuthSession.owner_id == scope.account_id))
    await session.execute(delete(GoogleIdentity).where(GoogleIdentity.owner_id == scope.account_id))
    if scope.account_id == 1:
        owner.password_hash = ""
        owner.email = None
        owner.email_verified_at = None
        owner.email_verification_source = None
        owner.created_at = datetime(1970, 1, 1, tzinfo=UTC)
    else:
        await session.delete(owner)
    operation.account_id = None
    operation.workspace_id = None
    operation.cursor = {}
    operation.status = "succeeded"
    operation.phase = "complete"
    operation.error_code = None
    operation.completed_at = datetime.now(UTC)
    operation.expires_at = operation.completed_at + timedelta(days=7)


async def process_account_deletion(ctx: dict[str, Any], operation_id: UUID) -> bool:
    """Advance one bounded stage under a session advisory lock, with durable safe failure.

    A dedicated connection retains only the advisory lock, never an open transaction during
    cleanup/provider I/O. Connection loss releases the lock. Owner cleanup is idempotent; each
    committed stage is checkpointed separately and failed work retries after one minute. The
    backend PID and locked operation checkpoint fence a connection lost during provider I/O.
    """
    from core.auth.deletion_cleanup import erase_account_data

    engine = cast(AsyncEngine, ctx["db_engine"])
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    key = int.from_bytes(operation_id.bytes[:8], "big", signed=True)
    async with engine.connect() as connection:
        backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        await connection.commit()
        if not acquired:
            return False
        phase: str | None = None
        cursor: dict[str, Any] = {}
        try:
            async with factory() as session:
                operation = await session.get(AccountDeletionOperation, operation_id)
                if operation is None or operation.status == "succeeded":
                    return False
                assert operation.account_id is not None and operation.workspace_id is not None
                scope = AccountErasureScope(operation.id, operation.account_id, operation.workspace_id)
                phase, cursor = operation.phase, dict(operation.cursor)
                await authorize_account_erasure(session, scope)
                operation.status = "running"
                operation.error_code = None
                await session.commit()
            # A cancelled/crashed stage remains durable and is picked up by the next sweep.
            progress = await erase_account_data(ctx, scope, phase, cursor)
            # SQLAlchemy may reconnect after provider I/O; a new backend no longer owns our lock.
            if await connection.scalar(text("SELECT pg_backend_pid()")) != backend_pid:
                raise RuntimeError("Account deletion lock connection changed")
            await connection.commit()
            async with factory() as session:
                operation = await session.scalar(select(AccountDeletionOperation).where(
                    AccountDeletionOperation.id == operation_id,
                ).with_for_update())
                if operation is None or operation.phase != phase or operation.cursor != cursor or operation.status != "running":
                    raise RuntimeError("Account deletion checkpoint changed during cleanup")
                if progress.complete:
                    await finalize_account_erasure(session, operation)
                else:
                    operation.phase, operation.cursor = progress.phase, progress.cursor
                    operation.next_attempt_at = datetime.now(UTC)
                await session.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the operation and retried
            async with factory() as session:
                operation = await session.get(AccountDeletionOperation, operation_id)
                if (operation is not None and operation.status in {"queued", "running"}
                        and operation.phase == phase and operation.cursor == cursor):
                    operation.status = "failed"
                    safe_code = getattr(exc, "safe_code", None)
                    operation.error_code = safe_code if safe_code in {
                        "auxiliary_cleanup_failed", "remote_cleanup_failed", "graph_cleanup_failed",
                        "storage_cleanup_failed", "shared_cleanup_failed", "data_cleanup_failed",
                    } else "cleanup_retry_required"
                    operation.next_attempt_at = datetime.now(UTC) + timedelta(minutes=1)
                    await session.commit()
            return False
        finally:
            # Never return a pooled connection carrying a session-level lock.
            try:
                await asyncio.shield(connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}))
                await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise


async def recover_account_deletions(ctx: dict[str, Any]) -> int:
    """Scan at startup/every thirty seconds; recover lost enqueue and purge expired receipts.

    At most four stages run per sweep. Failures retain status and original authority; terminal
    records alone expire after seven days, so a failed operation never silently disappears.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
    async with factory() as session:
        expired = select(AccountDeletionOperation.id).where(
            AccountDeletionOperation.status == "succeeded", AccountDeletionOperation.expires_at <= datetime.now(UTC),
        ).limit(100)
        await session.execute(delete(AccountDeletionOperation).where(AccountDeletionOperation.id.in_(expired)))
        ids = list((await session.scalars(select(AccountDeletionOperation.id).where(
            AccountDeletionOperation.status != "succeeded", AccountDeletionOperation.next_attempt_at <= datetime.now(UTC),
        ).order_by(AccountDeletionOperation.next_attempt_at).limit(4))).all())
        await session.commit()
    count = 0
    for operation_id in ids:
        count += await process_account_deletion(ctx, operation_id)
    return count
