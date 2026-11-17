import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core.errors import ValidationFailedError


def encode_cursor(created_at: datetime, identifier: UUID) -> str:
    """Encode a timestamp and UUID as canonical unpadded URL-safe base64 JSON for pagination."""
    value = json.dumps([created_at.isoformat(), str(identifier)], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode and validate a canonical timezone-aware timestamp/UUID cursor; convert malformed input to HTTP 422."""
    try:
        if "=" in cursor:
            raise ValueError("Cursor must be unpadded")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Cursor is not canonical URL-safe base64")
        value = json.loads(raw)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError("Cursor must contain a timestamp and UUID")
        timestamp, identifier = value
        parsed = datetime.fromisoformat(timestamp)
        if parsed.utcoffset() is None:
            raise ValueError("Cursor timestamp must include a timezone")
        return parsed, UUID(identifier)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise ValidationFailedError("Invalid cursor") from exc


# --- Fingerprinted cursors -------------------------------------------------------------------
# Search, news stories and observations bind continuation cursors to the query/admission
# state that produced them. These helpers hold the shared mechanics (canonical fingerprint,
# unpadded URL-safe base64, strict decode) so modules stop re-implementing them.

_COMPACT_SEPARATORS = (",", ":")


def fingerprint_payload(
    payload: Mapping[str, Any],
    *,
    length: int | None = None,
    separators: tuple[str, str] = _COMPACT_SEPARATORS,
) -> str:
    """Return the SHA-256 hex digest of ``payload`` serialised as sorted-key JSON.

    Args:
        payload: JSON-serialisable mapping binding filters, scopes and admission revisions.
        length: Optional hex prefix length (search uses 16); ``None`` keeps all 64 characters.
        separators: JSON separators. Search historically used the ``json.dumps`` defaults
            ``(", ", ": ")``; pass them to keep previously issued cursors valid.
    """
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=separators).encode()
    ).hexdigest()
    return digest if length is None else digest[:length]


def b64url_encode(raw: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(cursor: str) -> bytes:
    """Decode unpadded (or padded) URL-safe base64 strictly, rejecting stray characters.

    Raises:
        binascii.Error: For characters outside the URL-safe alphabet or bad lengths.
    """
    return base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)


def encode_fingerprinted_cursor(fingerprint: str, stamp: datetime, identifier: UUID) -> str:
    """Encode ``[fingerprint, UTC ISO timestamp, UUID]`` as a compact unpadded cursor."""
    payload = json.dumps(
        [fingerprint, stamp.astimezone(UTC).isoformat(), str(identifier)],
        separators=_COMPACT_SEPARATORS,
    )
    return b64url_encode(payload.encode())


def decode_fingerprinted_cursor(
    cursor: str,
    fingerprint: str,
    *,
    detail: str = "Invalid cursor",
    max_length: int = 2048,
) -> tuple[datetime, UUID]:
    """Decode a cursor and reject malformed input or reuse under a different fingerprint.

    Returns the timezone-aware UTC timestamp and canonical-string UUID. Every failure,
    including a fingerprint mismatch (a cursor replayed against other filters, workspaces or
    admission revisions), becomes HTTP 422 with ``detail``.
    """
    if len(cursor) > max_length:
        raise ValidationFailedError(detail)
    try:
        identity, stamp, row_id = json.loads(b64url_decode(cursor))
        if identity != fingerprint:
            raise ValueError("Cursor was issued for a different query")
        parsed = datetime.fromisoformat(stamp)
        parsed_id = UUID(row_id)
        if parsed.tzinfo is None or parsed.utcoffset() is None or str(parsed_id) != row_id:
            raise ValueError("Cursor timestamp or identifier is not canonical")
        return parsed.astimezone(UTC), parsed_id
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail) from exc


def encode_scoped_offset(scope: str, offset: int) -> str:
    """Encode ``<scope>:<offset>`` (scope is a fingerprint) as an unpadded URL-safe cursor."""
    return b64url_encode(f"{scope}:{offset}".encode())


def decode_scoped_offset(
    cursor: str, scope: str, *, max_offset: int, detail: str = "Invalid cursor"
) -> int:
    """Decode a scoped ranked offset, requiring the exact scope, bounds and canonical encoding.

    Raises:
        ValidationFailedError: 422 with ``detail`` for any malformed, out-of-range, cross-scope or
            non-canonical cursor.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        found, position = raw.split(":", 1)
        offset = int(position)
        if (
            found != scope
            or offset < 0
            or offset > max_offset
            or encode_scoped_offset(scope, offset) != cursor
        ):
            raise ValueError("Cursor does not match this query")
        return offset
    except (ValueError, UnicodeDecodeError, IndexError, binascii.Error) as exc:
        raise ValidationFailedError(detail) from exc
