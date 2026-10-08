"""Fixed identity cursors shared by scheduled workers: Redis first, startup-installed dict as fallback.

Lives in ``core`` because news and dashboard (and later search) share one ``ctx["w2_cursor_state"]``
object; neither module may import the other.

Shape of the shared dict (``dict[str, str]``, every value a plain string so other users of the
same object, e.g. search indexing, can treat it as such):
  * ``<key>``            -> canonical UUID string, or ``""`` for "start from the beginning"
  * ``_gen:<key>``       -> decimal write generation; guards against overlapping jobs
  * ``_unsynced:<key>``  -> ``"1"`` while the last Redis write failed (local value beats Redis)
Cursor keys must never start with ``_``.
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from typing import cast
from uuid import UUID

from redis.asyncio import Redis

_log = logging.getLogger(__name__)
STATE_KEY = "w2_cursor_state"
_GENS = "_cursor_read_gens"  # job-local (ARQ copies ctx per job): generation seen at read time


def _parse(raw: object) -> tuple[bool, UUID | None]:
    """Return (valid, cursor); empty means a valid "beginning" cursor."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    if not isinstance(raw, str):
        return False, None
    if not raw:
        return True, None
    try:
        return True, UUID(raw)
    except ValueError:
        return False, None


def _state(ctx: dict[str, object]) -> dict[str, str]:
    return cast(dict[str, str], ctx[STATE_KEY])


async def read_cursor(ctx: dict[str, object], key: str, allowed: Collection[str]) -> UUID | None:
    """Read a cursor; a malformed Redis value keeps the local one, an unsynced key skips Redis."""
    if key not in allowed:
        raise KeyError(key)
    state = _state(ctx)
    _, cursor = _parse(state.get(key))
    redis = cast(Redis | None, ctx.get("redis"))
    if redis is not None and f"_unsynced:{key}" not in state:
        try:
            remote = await redis.get(key)
            if remote is not None:
                remote_ok, remote_cursor = _parse(remote)
                if remote_ok:
                    cursor = remote_cursor
        except Exception:  # noqa: BLE001 - best-effort cursor store
            _log.debug("cursor read failed", exc_info=True)
    state[key] = str(cursor) if cursor is not None else ""
    cast(dict[str, str], ctx.setdefault(_GENS, {}))[key] = state.get(f"_gen:{key}", "0")
    return cursor


async def write_cursor(ctx: dict[str, object], key: str, cursor: UUID | None, allowed: Collection[str]) -> None:
    """Forward-only write: skipped when another job wrote this key after this job read it."""
    if key not in allowed:
        raise KeyError(key)
    state = _state(ctx)
    gens = cast(dict[str, str], ctx.setdefault(_GENS, {}))
    current = state.get(f"_gen:{key}", "0")
    if key in gens and gens[key] != current:
        return
    state[f"_gen:{key}"] = gens[key] = str(int(current) + 1)
    state[key] = str(cursor) if cursor is not None else ""
    redis = cast(Redis | None, ctx.get("redis"))
    if redis is None:
        return
    try:
        if cursor is None:
            await redis.delete(key)
        else:
            await redis.set(key, str(cursor))
        state.pop(f"_unsynced:{key}", None)
    except Exception:  # noqa: BLE001 - shared startup state retains progress
        _log.debug("cursor write failed", exc_info=True)
        state[f"_unsynced:{key}"] = "1"
