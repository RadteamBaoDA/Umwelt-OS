"""Fingerprinted cursor helpers stay wire-compatible with the module implementations they replace."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from core import pagination
from core.errors import ValidationFailedError

STAMP = datetime(2026, 5, 1, 12, 30, tzinfo=UTC)


def test_fingerprint_matches_legacy_encodings() -> None:
    """Compact/full (observations, stories) and default-separator/16-char (search) digests."""
    payload = {"b": [1, 2], "a": {"z": 1}}
    compact = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert pagination.fingerprint_payload(payload) == compact
    legacy = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    assert pagination.fingerprint_payload(payload, length=16, separators=(", ", ": ")) == legacy


def test_fingerprinted_cursor_roundtrip_and_observation_compat() -> None:
    """Encoded cursors decode through both the core helper and the observations module decoder."""
    from modules.knowledge.observations.public import _decode_cursor as legacy_decode

    identifier = uuid4()
    cursor = pagination.encode_fingerprinted_cursor(
        "fp", STAMP.astimezone(timezone(timedelta(hours=2))), identifier
    )
    assert "=" not in cursor
    assert pagination.decode_fingerprinted_cursor(cursor, "fp") == (STAMP, identifier)
    assert legacy_decode(cursor, "fp") == (STAMP, identifier)


def test_fingerprinted_cursor_rejects_cross_query_and_garbage() -> None:
    """Replayed, oversized, naive-timestamp and non-base64 cursors all yield 422 with the detail."""
    cursor = pagination.encode_fingerprinted_cursor("fp", STAMP, uuid4())
    naive = pagination.b64url_encode(
        json.dumps(["fp", "2026-05-01T12:00:00", str(uuid4())]).encode()
    )
    for bad in (cursor, "!!!", "A" * 3000, naive):
        with pytest.raises(ValidationFailedError) as failure:
            pagination.decode_fingerprinted_cursor(
                bad, "other" if bad == cursor else "fp", detail="nope"
            )
        assert failure.value.status_code == 422 and failure.value.detail == "nope"


def test_scoped_offset_roundtrip_and_bounds() -> None:
    """Scoped offsets are canonical, bounded and scope-bound; padded forms are rejected."""
    cursor = pagination.encode_scoped_offset("abc123", 40)
    assert pagination.decode_scoped_offset(cursor, "abc123", max_offset=100) == 40
    # Byte-identical to the search module's historical encoding.
    assert cursor == base64.urlsafe_b64encode(b"abc123:40").decode().rstrip("=")
    for bad, scope, bound in (
        (cursor, "other", 100),
        (cursor, "abc123", 10),
        (cursor + "=", "abc123", 100),
        (pagination.encode_scoped_offset("abc123", -1), "abc123", 100),
    ):
        with pytest.raises(ValidationFailedError) as failure:
            pagination.decode_scoped_offset(
                bad, scope, max_offset=bound, detail="Invalid search cursor"
            )
        assert failure.value.status_code == 422 and failure.value.detail == "Invalid search cursor"
