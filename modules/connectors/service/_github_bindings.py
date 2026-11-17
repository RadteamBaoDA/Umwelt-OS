"""GitHub binding-page cursor helpers split out of ``github_hints`` to keep it within its size ratchet."""

import base64
import json
from uuid import UUID

from core.pagination import b64url_encode


def _decode_binding_cursor(
    cursor: str | None, app_id: str, installation_id: str, repository_id: str | None
) -> UUID | None:
    """Decode an opaque binding-page cursor into its keyset position; ``None`` when no cursor was given.

    Raises ``ValueError`` when the cursor is malformed or was issued for a different App, installation or
    repository query, so a cursor can never page a different enumeration.
    """
    if cursor is None:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (
            decoded.get("app") != app_id
            or decoded.get("installation") != installation_id
            or decoded.get("repository") != repository_id
        ):
            raise ValueError
        return UUID(decoded["after"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub binding cursor is invalid") from exc


def _encode_binding_cursor(
    app_id: str, installation_id: str, repository_id: str | None, last_source_id: UUID
) -> str:
    """Encode the keyset position after ``last_source_id`` bound to the exact App/installation/repository query.

    Raises ``ValueError`` when the encoded cursor would exceed its 512-character bound.
    """
    token = json.dumps(
        {
            "app": app_id,
            "installation": installation_id,
            "repository": repository_id,
            "after": str(last_source_id),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    next_cursor = b64url_encode(token)
    if len(next_cursor) > 512:
        raise ValueError("GitHub binding cursor exceeds its bound")
    return next_cursor
