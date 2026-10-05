"""Public owner-authentication checks for other modules.

This module keeps AuthSession persistence private while accepting only detached token and owner
identifiers from callers. Callers own the short session lifetime and must not retain it over I/O.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import AuthSession, Owner


async def revalidate_owner_session(
    session: AsyncSession,
    token_hash: str,
    owner_id: int,
) -> bool:
    """Return whether the exact owner session remains unexpired in this fresh short transaction.

    The caller supplies only its authenticated session's stored token digest and owner ID. This
    query grants no authority by itself, performs no write, and returns false for revoked, expired
    or differently owned sessions; AuthSession never crosses the public module contract.
    """
    current = await session.scalar(
        select(AuthSession.token_hash).where(
            AuthSession.token_hash == token_hash,
            AuthSession.owner_id == owner_id,
            AuthSession.expires_at > datetime.now(UTC),
        )
    )
    return current is not None


async def get_demo_owner_id(session: AsyncSession) -> int:
    """Return the configured singleton owner ID, failing when setup has not created it."""
    owner_id = await session.scalar(select(Owner.id).where(Owner.id == 1))
    if owner_id is None:
        raise RuntimeError("Demo seeding requires the configured owner account")
    return owner_id


__all__ = ["get_demo_owner_id", "revalidate_owner_session"]
