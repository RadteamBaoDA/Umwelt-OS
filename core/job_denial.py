"""Denial-is-terminal helpers for durable worker jobs (W4-jobs-k).

A typed admission denial (401/403/404 -> ``permission_lost``; 409 that survives one
re-resolve -> ``stale_scope``) ends a job in its owner's EXISTING terminal status. No new
statuses; rows already past the point of an unknown external effect are never touched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PERMISSION_LOST = "permission_lost"
STALE_SCOPE = "stale_scope"


def denial_code(exc: BaseException) -> str | None:
    """Map a typed admission denial to its terminal error code; None for anything else."""
    if not isinstance(exc, HTTPException):
        return None
    if exc.status_code in {401, 403, 404}:
        return PERMISSION_LOST
    return STALE_SCOPE if exc.status_code == 409 else None


async def admit_retry_stale[T](admit: Callable[[], Awaitable[T]]) -> T:
    """Run ``admit``; a 409 is re-resolved exactly once (the second outcome is final)."""
    try:
        return await admit()
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
    return await admit()


async def terminalize(
    factory: async_sessionmaker[AsyncSession], model: Any, row_id: UUID, workspace_id: UUID, code: str,
    *, from_status: Iterable[str] = ("pending", "running", "blocked"), failed: str = "failed",
    extra_where: tuple[Any, ...] = (),
) -> None:
    """Move one durable row to its owner's terminal ``failed`` state, workspace-predicated.

    Direct UPDATE, no access fence: the principal is denied, so there is nothing to publish.
    """
    values: dict[str, Any] = {"status": failed, "error_code": code}
    for column in ("lease_owner", "lease_expires_at"):
        if hasattr(model, column):
            values[column] = None
    async with factory() as session:
        await session.execute(update(model).where(
            model.id == row_id, model.workspace_id == workspace_id,
            model.status.in_(tuple(from_status)), *extra_where,
        ).values(**values))
        await session.commit()
