"""Bounded erasure of account-owned caches and saver state outside declarative tables."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.auth.models import GoogleIdentity, Owner
from modules.agents.public import erase_account_checkpoints_page


def _owned_cache_key(key: str, scope: AccountErasureScope, mailboxes: set[str]) -> bool:
    """Recognize exact app-owned account/workspace namespaces, never substring-match peer IDs.

    Deployment-wide capacity leases and anonymous IP counters are intentionally excluded:
    releasing another worker's lease could permit overlapping remote effects.
    """
    workspace = str(scope.workspace_id)
    prefixes = (
        f'bbd:model-gateway:capability:w:{workspace}:u:{scope.account_id}:g:',
        f'dashboard:brief-attempt:{workspace}:',
        f'chat:websearch:{scope.account_id}:',
        f'chat:websearch:{workspace}:{scope.account_id}:',
    )
    if key.startswith(prefixes) or key in {
        f'cache:memory:active:{workspace}',
        f'bbd:entity-extraction:ready-cursor:{workspace}',
        f'bbd:timeline-extraction:ready-cursor:{workspace}',
    }:
        return True
    identity = hashlib.sha256(f'account:{scope.account_id}'.encode()).hexdigest()[:32]
    parts = key.split(':')
    if len(parts) == 4 and parts[0] == 'budget' and parts[2] == identity:
        return True
    identifiers = mailboxes | ({''} if scope.account_id == 1 else set())
    return any(key.startswith((
        f'auth:login:fails:{hashlib.sha256(mailbox.encode()).hexdigest()[:24]}:',
        f'auth:login:lock:{hashlib.sha256(mailbox.encode()).hexdigest()[:24]}:',
    )) for mailbox in identifiers)


async def erase_auxiliary_data(
    ctx: dict[str, Any], scope: AccountErasureScope, cursor: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Erase one saver page or one Redis SCAN page and return a durable continuation.

    Saver writes are excluded by per-run PostgreSQL leases inside the owning Agent contract.
    SQL preparation ends before Redis I/O. Redis unavailability and malformed oversized OAuth
    state fail the required stage; peer keys and global capacity leases are preserved. Only
    numeric SCAN progress and a checkpoint-completed marker enter the operation cursor.
    """
    factory = cast(async_sessionmaker[AsyncSession], ctx['session_factory'])
    async with factory() as session:
        await authorize_account_erasure(session, scope)
        if cursor.get('checkpoints') is not True:
            done = await erase_account_checkpoints_page(session, scope)
            await session.commit()
            return False, {'checkpoints': True} if done else {}
        email = await session.scalar(select(Owner.email).where(Owner.id == scope.account_id))
        google_email = await session.scalar(select(GoogleIdentity.email).where(GoogleIdentity.owner_id == scope.account_id))
        mailboxes = {value for value in (email, google_email) if isinstance(value, str)}
        await session.rollback()
    scan_cursor = cursor.get('redis', 0)
    if type(scan_cursor) is not int or scan_cursor < 0:
        raise ValueError('Invalid Redis erasure continuation')
    redis = cast(Redis, ctx['redis'])
    next_cursor, keys = await redis.scan(cursor=scan_cursor, count=200)
    owned = []
    for raw_key in keys:
        key = raw_key.decode('utf8') if isinstance(raw_key, bytes) else raw_key
        if not isinstance(key, str):
            continue
        if _owned_cache_key(key, scope, mailboxes):
            owned.append(raw_key)
        elif key.startswith(('auth:google:state:', 'auth:google:enrollment:')):
            raw = await redis.get(raw_key)
            if raw is not None:
                if len(raw) > 8192:
                    raise RuntimeError('OAuth cleanup state exceeds its expected bound')
                value = json.loads(raw)
                if isinstance(value, dict) and (
                    value.get('owner_id') == scope.account_id or value.get('email') in mailboxes
                ):
                    owned.append(raw_key)
    if owned:
        await redis.delete(*owned)
    # Remove account-owned discovery hints in this worker process as well as Redis copies.
    hints = ctx.get('w2_cursor_state')
    if isinstance(hints, dict):
        for key in list(hints):
            if str(scope.workspace_id) in key or hints[key] == str(scope.workspace_id):
                hints.pop(key, None)
    return int(next_cursor) == 0, {'checkpoints': True, 'redis': int(next_cursor)}
