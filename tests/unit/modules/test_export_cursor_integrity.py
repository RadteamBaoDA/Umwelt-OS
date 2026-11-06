"""Export keyset cursors must round-trip and refuse tampering, scope substitution and future cutoffs.

Covers the chat, timeline, memory and dashboard export cursors that bind a page position to its owner,
dataset and snapshot cutoff.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from core.errors import ValidationFailedError
from core.pagination import b64url_encode
from core.workspaces.schemas import AccessFence
from modules.chat import _public_export as chat_export
from modules.dashboard import _export as dash_export
from modules.memory import _export as memory_export
from modules.timeline import _export as timeline_export

SNAPSHOT = datetime(2026, 1, 10, 12, tzinfo=UTC)
POSITION = SNAPSHOT - timedelta(hours=1)
ROW_ID = UUID("00000000-0000-0000-0000-000000000042")
WORKSPACE = UUID("00000000-0000-0000-0000-0000000000aa")
FENCE = AccessFence(
    workspace_id=WORKSPACE, user_id=7, membership_revision=3, configuration_revision=5
)


def _tamper(cursor: str) -> str:
    """Flip the last character so the canonical re-encoding check fails."""
    return cursor[:-1] + ("A" if cursor[-1] != "A" else "B")


def test_chat_cursor_round_trip_and_binding() -> None:
    cursor = chat_export._encode_chat_export_cursor(1, "messages", SNAPSHOT, POSITION, ROW_ID)
    assert chat_export._decode_chat_export_cursor(cursor, 1, "messages") == (
        SNAPSHOT,
        POSITION,
        ROW_ID,
    )
    for owner, kind in ((2, "messages"), (1, "conversations")):
        with pytest.raises(ValueError, match="Invalid chat export cursor"):
            chat_export._decode_chat_export_cursor(cursor, owner, kind)
    with pytest.raises(ValueError):
        chat_export._decode_chat_export_cursor(_tamper(cursor), 1, "messages")


@pytest.mark.parametrize("cursor", ["", "a" * 1025, "abc=", "not-base64!!"])
def test_chat_cursor_rejects_malformed_input(cursor: str) -> None:
    with pytest.raises(ValueError, match="Invalid chat export cursor"):
        chat_export._decode_chat_export_cursor(cursor, 1, "messages")


def test_chat_cursor_rejects_future_cutoff_and_naive_times() -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    cursor = chat_export._encode_chat_export_cursor(1, "messages", future, POSITION, ROW_ID)
    with pytest.raises(ValueError):
        chat_export._decode_chat_export_cursor(cursor, 1, "messages")
    payload = {
        "v": 1,
        "owner": 1,
        "kind": "messages",
        "snapshot": "2026-01-01T00:00:00",
        "at": "2026-01-01T00:00:00",
        "id": str(ROW_ID),
    }
    naive = b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    with pytest.raises(ValueError):
        chat_export._decode_chat_export_cursor(naive, 1, "messages")


def test_chat_cursor_rejects_extra_or_missing_keys() -> None:
    payload = {
        "v": 1,
        "owner": 1,
        "kind": "messages",
        "snapshot": SNAPSHOT.isoformat(),
        "at": POSITION.isoformat(),
    }
    cursor = b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    with pytest.raises(ValueError):
        chat_export._decode_chat_export_cursor(cursor, 1, "messages")


def test_chat_export_url_is_stripped_to_credential_free_web_url() -> None:
    safe = chat_export._safe_chat_export_url
    assert safe("https://example.com/a/b?token=1#frag") == "https://example.com/a/b"
    assert safe("http://example.com:8080/p") == "http://example.com:8080/p"
    assert safe("https://[::1]:9000/x") == "https://[::1]:9000/x"
    for bad in (
        None,
        "",
        "ftp://example.com/x",
        "https://user:pw@example.com/x",
        "https://user@example.com/",
        "javascript:alert(1)",
        "https://example.com:notaport/",
    ):
        assert safe(bad) is None


def test_chat_export_byte_measures_match_compact_utf8_json() -> None:
    from pydantic import BaseModel

    class Item(BaseModel):
        text: str

    items = [Item(text="é"), Item(text="b")]
    assert chat_export._chat_export_item_bytes(items[0]) == len('{"text":"é"}'.encode())
    assert chat_export._chat_export_payload_bytes(items) == len(
        '[{"text":"é"},{"text":"b"}]'.encode()
    )


def test_chat_export_owner_rule() -> None:
    from types import SimpleNamespace

    owner = lambda uid: SimpleNamespace(user_id=uid, role="owner")  # noqa: E731
    assert chat_export._export_owner_allowed(1, owner(1), False) is True
    assert chat_export._export_owner_allowed(2, owner(2), False) is False  # rollout off
    assert chat_export._export_owner_allowed(2, owner(2), True) is True
    assert (
        chat_export._export_owner_allowed(2, owner(3), True) is False
    )  # scope belongs to someone else
    assert (
        chat_export._export_owner_allowed(2, SimpleNamespace(user_id=2, role="member"), True)
        is False
    )


def test_timeline_cursor_round_trip_and_binding() -> None:
    cursor = timeline_export._encode_timeline_export_cursor(
        1, WORKSPACE, SNAPSHOT, POSITION, ROW_ID
    )
    assert timeline_export._decode_timeline_export_cursor(cursor, 1, WORKSPACE) == (
        SNAPSHOT,
        POSITION,
        ROW_ID,
    )
    for owner, workspace in ((2, WORKSPACE), (1, uuid4())):
        with pytest.raises(ValueError, match="Invalid timeline export cursor"):
            timeline_export._decode_timeline_export_cursor(cursor, owner, workspace)
    with pytest.raises(ValueError):
        timeline_export._decode_timeline_export_cursor(_tamper(cursor), 1, WORKSPACE)
    for bad in ("", "x" * 1025, "a="):
        with pytest.raises(ValueError):
            timeline_export._decode_timeline_export_cursor(bad, 1, WORKSPACE)


def test_timeline_cursor_rejects_future_cutoff() -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    cursor = timeline_export._encode_timeline_export_cursor(1, WORKSPACE, future, POSITION, ROW_ID)
    with pytest.raises(ValueError):
        timeline_export._decode_timeline_export_cursor(cursor, 1, WORKSPACE)


def test_timeline_export_json_is_canonical() -> None:
    assert timeline_export._timeline_export_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_memory_cursor_round_trip_and_binding() -> None:
    cursor = memory_export._encode_memory_export_cursor(1, "memories", SNAPSHOT, POSITION, ROW_ID)
    assert memory_export._decode_memory_export_cursor(cursor, 1, "memories") == (
        SNAPSHOT,
        POSITION,
        ROW_ID,
    )
    for owner, kind in ((2, "memories"), (1, "candidates")):
        with pytest.raises(ValidationFailedError):
            memory_export._decode_memory_export_cursor(cursor, owner, kind)
    with pytest.raises(ValidationFailedError):
        memory_export._decode_memory_export_cursor(_tamper(cursor), 1, "memories")


def test_memory_cursor_rejects_position_after_snapshot_and_future_cutoff() -> None:
    inverted = memory_export._encode_memory_export_cursor(1, "memories", POSITION, SNAPSHOT, ROW_ID)
    with pytest.raises(ValidationFailedError):
        memory_export._decode_memory_export_cursor(inverted, 1, "memories")
    future = datetime.now(UTC) + timedelta(days=1)
    cursor = memory_export._encode_memory_export_cursor(1, "memories", future, POSITION, ROW_ID)
    with pytest.raises(ValidationFailedError):
        memory_export._decode_memory_export_cursor(cursor, 1, "memories")
    for bad in ("x" * 513, "a=", ""):
        with pytest.raises(ValidationFailedError):
            memory_export._decode_memory_export_cursor(bad, 1, "memories")


def test_dashboard_cursor_round_trip_and_fence_binding() -> None:
    cursor = dash_export._encode_dashboard_export_cursor(SNAPSHOT, POSITION, ROW_ID, FENCE)
    assert dash_export._decode_dashboard_export_cursor(cursor, FENCE) == (
        SNAPSHOT,
        POSITION,
        ROW_ID,
    )
    for changed in (
        replace(FENCE, membership_revision=4),
        replace(FENCE, configuration_revision=6),
        replace(FENCE, user_id=8),
        replace(FENCE, workspace_id=uuid4()),
    ):
        with pytest.raises(ValidationFailedError, match="Dashboard export cursor"):
            dash_export._decode_dashboard_export_cursor(cursor, changed)
    with pytest.raises(ValidationFailedError):
        dash_export._decode_dashboard_export_cursor(_tamper(cursor), FENCE)


def test_owner_cursor_is_bound_to_record_kind_and_sort() -> None:
    cursor = dash_export._encode_owner_export_cursor(
        "daily_briefs", SNAPSHOT, POSITION, ROW_ID, FENCE
    )
    assert dash_export._decode_owner_export_cursor(cursor, "daily_briefs", FENCE) == (
        SNAPSHOT,
        POSITION,
        ROW_ID,
    )
    with pytest.raises(ValidationFailedError, match="Owner export cursor"):
        dash_export._decode_owner_export_cursor(cursor, "gadget_definitions", FENCE)
    raw_kind = dash_export._encode_owner_export_cursor(
        "gadget_definitions", SNAPSHOT, POSITION, ROW_ID, FENCE
    )
    assert (
        dash_export._decode_owner_export_cursor(raw_kind, "gadget_definitions", FENCE)[2] == ROW_ID
    )
    future = datetime.now(UTC) + timedelta(days=1)
    stale = dash_export._encode_owner_export_cursor("daily_briefs", future, POSITION, ROW_ID, FENCE)
    with pytest.raises(ValidationFailedError):
        dash_export._decode_owner_export_cursor(stale, "daily_briefs", FENCE)
    for bad in ("", "a=", "x" * 1025):
        with pytest.raises(ValidationFailedError):
            dash_export._decode_owner_export_cursor(bad, "daily_briefs", FENCE)
