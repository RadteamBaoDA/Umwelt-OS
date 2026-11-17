"""Shared building blocks for bounded, snapshot-fenced portable export pages.

Every module that feeds ``modules/export`` exposes ``export_page`` and ``validate_export_fences``
with the same skeleton: a keyset cursor bound to an immutable snapshot cutoff, a byte-bounded
page of detached DTOs, and one content digest per record that the final validation pass
recomputes. The pieces below are the parts that were copy-pasted verbatim; modules keep their own
queries, DTOs and fence models.

Adopted by: ``modules.goals.public``, ``modules.tasks.public`` and ``modules.dashboard.public``
(byte bound only; its cursors also bind admission revisions), ``modules.news.topics`` and
``modules.sources.public`` (byte bound + digests; their cursors bind admission/filter fingerprints) and
``modules.memory.public`` (digests only; it tracks omitted records while paging).

Divergent copies kept on purpose (different cursor grammar and/or digest input, see
``docs/architecture/workspace-access-helpers.md``); the old line numbers are historical:
``modules/timeline/public.py`` (245, 298), ``modules/chat/public.py`` (1102, 1334),
``modules/knowledge/relationships/public.py`` (1609, 1689), ``modules/knowledge/observations/public.py``
(179, 267), ``modules/knowledge/entities/public.py`` (236, 400), ``modules/knowledge/documents/public.py``
(388, 604) and ``modules/memory/public.py`` (417, 487; it adopted only the digest helpers above).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from core.errors import PayloadTooLargeError, ValidationFailedError

EXPORT_PAGE_BYTES = 16_777_216
"""Upper bound of one serialized export page (JSON array of DTOs)."""

_MAX_CURSOR_CHARS = 512


class _JsonDump(Protocol):
    """Anything exposing pydantic's ``model_dump_json``."""

    def model_dump_json(self) -> str:
        """Serialize to a JSON string."""


def encode_snapshot_cursor(
    dataset: str,
    snapshot_at: datetime,
    position_at: datetime,
    identifier: UUID,
) -> str:
    """Bind a ``(position_at, id)`` keyset position to one dataset and an immutable export cutoff.

    The result is an unpadded URL-safe base64 of ``[1, dataset, snapshot, position, id]`` with
    compact JSON, so it is canonical and can be re-derived by ``decode_snapshot_cursor``.
    """
    raw = json.dumps(
        [1, dataset, snapshot_at.isoformat(), position_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_snapshot_cursor(
    cursor: str,
    dataset: str,
    *,
    label: str,
) -> tuple[datetime, datetime, UUID]:
    """Decode a cursor made by ``encode_snapshot_cursor`` for ``dataset``.

    Rejects oversized, padded, non-canonical, cross-dataset, naive-timestamp and future-cutoff
    cursors (and positions after the cutoff) with a 422 ``"<label> export cursor is invalid"``.

    Returns:
        ``(snapshot_at, position_at, identifier)``.
    """
    try:
        if len(cursor) > _MAX_CURSOR_CHARS or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 5 or values[:2] != [1, dataset]:
            raise ValueError
        snapshot_at, position_at = (
            datetime.fromisoformat(values[2]),
            datetime.fromisoformat(values[3]),
        )
        identifier = UUID(values[4])
        if (
            any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (snapshot_at, position_at)
            )
            or snapshot_at.isoformat() != values[2]
            or position_at.isoformat() != values[3]
            or position_at > snapshot_at
            or snapshot_at > datetime.now(UTC)
            or str(identifier) != values[4]
            or encode_snapshot_cursor(dataset, snapshot_at, position_at, identifier) != cursor
        ):
            raise ValueError
        return snapshot_at, position_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValidationFailedError(detail=f"{label} export cursor is invalid") from exc


def serialize_bounded_page(
    items: Sequence[_JsonDump],
    *,
    label: str,
    max_bytes: int = EXPORT_PAGE_BYTES,
) -> tuple[list[bytes], int]:
    """Serialize each DTO once and enforce the page byte bound.

    Returns ``(encoded_items, payload_bytes)`` where ``payload_bytes`` is the size of the JSON array
    the items form (brackets plus separators). The per-item bytes are what ``content_digest`` hashes,
    so the digest and the size always describe the same serialization. Raises 413
    ``"<label> export page exceeds its byte bound"`` when over ``max_bytes``.
    """
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > max_bytes:
        raise PayloadTooLargeError(detail=f"{label} export page exceeds its byte bound")
    return encoded, payload_bytes


def content_digest(raw: bytes) -> str:
    """Return the hex SHA-256 recorded in a record's export fence and recomputed at validation."""
    return hashlib.sha256(raw).hexdigest()


def dto_digest(item: Any) -> str:
    """Digest a DTO exactly as ``serialize_bounded_page`` would, for the final validation pass."""
    return content_digest(item.model_dump_json().encode("utf-8"))
