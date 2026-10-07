"""Unit tests for core pagination cursor encoding and decoding.

Tests canonical unpadded base64 cursor encoding, roundtrip decoding,
and comprehensive error conditions resulting in HTTP 422 responses.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from core.pagination import decode_cursor, encode_cursor


class TestPaginationCursor:
    """Test suite for cursor encoding and decoding logic."""

    def test_encode_decode_roundtrip_utc(self) -> None:
        """encode_cursor and decode_cursor successfully roundtrip a UTC timestamp and UUID."""
        now = datetime.now(UTC)
        item_id = uuid4()

        cursor = encode_cursor(now, item_id)
        assert isinstance(cursor, str)
        assert "=" not in cursor

        decoded_dt, decoded_id = decode_cursor(cursor)
        assert decoded_id == item_id
        assert decoded_dt == now
        assert decoded_dt.tzinfo is not None

    def test_encode_decode_roundtrip_offset_timezone(self) -> None:
        """encode_cursor and decode_cursor preserve timezone offsets."""
        tz_offset = timezone(timedelta(hours=7, minutes=30))
        timestamp = datetime(2026, 10, 5, 14, 30, 45, 123456, tzinfo=tz_offset)
        item_id = uuid4()

        cursor = encode_cursor(timestamp, item_id)
        decoded_dt, decoded_id = decode_cursor(cursor)

        assert decoded_id == item_id
        assert decoded_dt == timestamp
        assert decoded_dt.utcoffset() == timedelta(hours=7, minutes=30)

    def test_encode_cursor_produces_unpadded_urlsafe_base64(self) -> None:
        """encode_cursor strips trailing padding '=' and uses urlsafe characters."""
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        item_id = UUID("12345678-1234-5678-1234-567812345678")

        cursor = encode_cursor(dt, item_id)
        assert not cursor.endswith("=")
        assert "/" not in cursor
        assert "+" not in cursor

    def test_decode_cursor_rejects_padded_cursor(self) -> None:
        """decode_cursor raises HTTP 422 if the cursor contains '=' padding."""
        dt = datetime.now(UTC)
        item_id = uuid4()
        value = json.dumps([dt.isoformat(), str(item_id)], separators=(",", ":"))
        padded = base64.urlsafe_b64encode(value.encode()).decode()

        # If padding happens to be present, test rejection directly
        cursor_with_padding = padded if padded.endswith("=") else padded + "="
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(cursor_with_padding)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "Invalid cursor"

    def test_decode_cursor_rejects_empty_string(self) -> None:
        """decode_cursor raises HTTP 422 on an empty string."""
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor("")
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_invalid_base64(self) -> None:
        """decode_cursor raises HTTP 422 for invalid base64 characters."""
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor("!@#$%^&*()")
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_standard_base64_chars(self) -> None:
        """decode_cursor raises HTTP 422 for standard base64 strings containing '+' or '/'."""
        with pytest.raises(HTTPException) as exc_info:
            decode_cursor("abc+def/123")
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_invalid_json(self) -> None:
        """decode_cursor raises HTTP 422 if decoded bytes do not form valid JSON."""
        non_json = b"not a json string at all"
        cursor = base64.urlsafe_b64encode(non_json).decode().rstrip("=")

        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(cursor)
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_json_non_list(self) -> None:
        """decode_cursor raises HTTP 422 if the JSON structure is a dict or primitive."""
        for payload in ({"ts": "2026-01-01T00:00:00Z", "id": str(uuid4())}, "single_string", 12345):
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            with pytest.raises(HTTPException) as exc_info:
                decode_cursor(encoded)
            assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_json_list_wrong_length(self) -> None:
        """decode_cursor raises HTTP 422 if list length is not exactly 2."""
        now = datetime.now(UTC).isoformat()
        uid = str(uuid4())

        for wrong_list in ([], [now], [now, uid, "extra_element"]):
            encoded = base64.urlsafe_b64encode(json.dumps(wrong_list).encode()).decode().rstrip("=")
            with pytest.raises(HTTPException) as exc_info:
                decode_cursor(encoded)
            assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_non_string_elements(self) -> None:
        """decode_cursor raises HTTP 422 if list elements are not both strings."""
        now = datetime.now(UTC).isoformat()
        uid = str(uuid4())

        for invalid_items in ([12345, uid], [now, 67890], [None, None]):
            encoded = base64.urlsafe_b64encode(json.dumps(invalid_items).encode()).decode().rstrip("=")
            with pytest.raises(HTTPException) as exc_info:
                decode_cursor(encoded)
            assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_malformed_timestamp(self) -> None:
        """decode_cursor raises HTTP 422 if timestamp string cannot be parsed as ISO datetime."""
        payload = ["invalid-date-string", str(uuid4())]
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(encoded)
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_naive_timestamp(self) -> None:
        """decode_cursor raises HTTP 422 if timestamp is naive (lacks timezone)."""
        naive_str = "2026-10-05T12:00:00"  # No 'Z' or offset
        payload = [naive_str, str(uuid4())]
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(encoded)
        assert exc_info.value.status_code == 422

    def test_decode_cursor_rejects_malformed_uuid(self) -> None:
        """decode_cursor raises HTTP 422 if identifier string is not a valid UUID."""
        now = datetime.now(UTC).isoformat()
        payload = [now, "not-a-valid-uuid-format"]
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        with pytest.raises(HTTPException) as exc_info:
            decode_cursor(encoded)
        assert exc_info.value.status_code == 422
