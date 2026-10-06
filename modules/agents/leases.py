"""Shared PostgreSQL run-lease identity for Agent workers and cleanup transactions."""

import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def agent_run_lease_key(run_id: UUID) -> int:
    """Derive the signed session-advisory key used to exclude one durable Agent run."""
    raw = int.from_bytes(hashlib.blake2b(run_id.bytes, digest_size=8, person=b"bbd-agent").digest(), "big")
    return raw if raw < 2**63 else raw - 2**64


async def try_agent_run_lease_in_uow(session: AsyncSession, run_id: UUID) -> bool:
    """Acquire a nonblocking transaction lease that conflicts with the worker's session lease.

    The lock remains held until the caller commits or rolls back its transaction, so a caller can
    delete checkpoint rows only when the active saver cannot write them again before commit.
    """
    key = agent_run_lease_key(run_id)
    return bool(await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}))
