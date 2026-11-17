"""Shared timeline admission, paging constants, day windows and cursor helpers (private to the timeline package)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_encode
from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.admission import ALL_SCOPE_TYPES, admit_scope
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before event locks.

    Thin wrapper over ``core.workspaces.admission.admit_scope`` (typed scopes labelled ``timeline workspace``,
    admin viewers read-only); keeps the module-private name. No commit; the caller owns the transaction.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        allowed_scopes=ALL_SCOPE_TYPES,
        scope_label="timeline workspace",
        admin_read_only_on_lock=True,
    )


MAX_PAGE = 100
TIMELINE_EXPORT_PAGE_MAX_BYTES = 16_777_216


def day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Return local calendar-day boundaries as UTC using independent zone conversions.

    Folded local midnight chooses its earliest valid instant. A skipped or
    nonexistent midnight raises ValueError rather than shifting the boundary.
    """
    zone = ZoneInfo(timezone)

    def boundary(value: date) -> datetime:
        """Resolve a local midnight and verify that the timezone round-trip preserves it."""
        naive = datetime.combine(value, time.min)
        candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        valid = [
            item
            for item in candidates
            if item.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
        ]
        if not valid:
            raise ValueError("calendar boundary is nonexistent in the requested timezone")
        return min(valid, key=lambda item: item.astimezone(UTC)).astimezone(UTC)

    return boundary(day), boundary(day + timedelta(days=1))


def _cursor_encode(value: dict[str, object]) -> str:
    """Encode bounded canonical JSON into an opaque URL-safe cursor."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return b64url_encode(raw)


def _cursor_decode(value: str | None, fingerprint: str) -> dict[str, Any]:
    """Validate cursor version and normalized-filter fingerprint before paging."""
    if not value:
        return {"v": 1, "f": fingerprint, "p": 0, "k": None}
    if len(value) > 1024:
        raise ValueError("cursor is too long")
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("f") != fingerprint:
        raise ValueError("cursor does not match this query")
    if type(payload.get("p")) is not int or payload["p"] not in range(3):
        raise ValueError("cursor partition is invalid")
    key = payload.get("k")
    if key is not None and (
        not isinstance(key, list) or len(key) != 2 or not all(isinstance(item, str) for item in key)
    ):
        raise ValueError("cursor key is malformed")
    if key is not None:
        try:
            if str(UUID(key[1])) != key[1]:
                raise ValueError("cursor ID is not canonical")
            if payload["p"] == 1:
                parsed_date = date.fromisoformat(key[0])
                if parsed_date.isoformat() != key[0]:
                    raise ValueError("cursor date is not canonical")
            else:
                parsed_instant = datetime.fromisoformat(key[0])
                if parsed_instant.tzinfo is None or parsed_instant.utcoffset() is None:
                    raise ValueError("cursor timestamp must be aware")
                if parsed_instant is None:
                    raise RuntimeError("invariant violated: parsed_instant must not be None")
                if (
                    parsed_instant.utcoffset() != timedelta(0)
                    or parsed_instant.astimezone(UTC).isoformat() != key[0]
                ):
                    raise ValueError("cursor timestamp is not canonical UTC")
        except (ValueError, TypeError) as exc:
            raise ValueError("cursor key is malformed") from exc
    return payload
