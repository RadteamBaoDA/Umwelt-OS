"""Shared export-page helpers: cursor grammar, byte bound and digests stay wire-compatible."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel

from core.errors import PayloadTooLargeError, ValidationFailedError
from core.export_pages import (
    content_digest,
    decode_snapshot_cursor,
    dto_digest,
    encode_snapshot_cursor,
    serialize_bounded_page,
)
from modules.goals import public as goals
from modules.tasks import public as tasks


class _Dto(BaseModel):
    value: str


def _legacy_cursor(
    dataset: str, snapshot_at: datetime, created_at: datetime, identifier: object
) -> str:
    """The pre-extraction goal/task encoding, reproduced literally to pin wire compatibility."""
    raw = json.dumps(
        [1, dataset, snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_cursor_round_trips_and_matches_legacy_encoding() -> None:
    snapshot = datetime.now(UTC) - timedelta(minutes=1)
    created = snapshot - timedelta(hours=1)
    identifier = uuid4()
    cursor = encode_snapshot_cursor("goals", snapshot, created, identifier)
    assert cursor == _legacy_cursor("goals", snapshot, created, identifier)
    assert decode_snapshot_cursor(cursor, "goals", label="Goal") == (snapshot, created, identifier)
    assert goals._decode_goal_export_cursor(
        goals._encode_goal_export_cursor(snapshot, created, identifier)
    ) == (snapshot, created, identifier)
    assert tasks._decode_task_export_cursor(
        tasks._encode_task_export_cursor(snapshot, created, identifier)
    ) == (snapshot, created, identifier)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c + "=",
        lambda c: "x" * 513,
        lambda c: c[:-2],
        lambda c: "not-base64!",
    ],
)
def test_cursor_rejects_malformed_values(mutate) -> None:
    snapshot = datetime.now(UTC) - timedelta(minutes=1)
    cursor = encode_snapshot_cursor("tasks", snapshot, snapshot, uuid4())
    with pytest.raises(ValidationFailedError) as caught:
        decode_snapshot_cursor(mutate(cursor), "tasks", label="Task")
    assert (
        caught.value.status_code == 422 and caught.value.detail == "Task export cursor is invalid"
    )


def test_cursor_rejects_other_dataset_future_cutoff_and_late_position() -> None:
    now = datetime.now(UTC)
    cursor = encode_snapshot_cursor(
        "tasks", now - timedelta(minutes=1), now - timedelta(minutes=2), uuid4()
    )
    with pytest.raises(ValidationFailedError):
        decode_snapshot_cursor(cursor, "goals", label="Goal")
    future = encode_snapshot_cursor("goals", now + timedelta(hours=1), now, uuid4())
    with pytest.raises(ValidationFailedError):
        decode_snapshot_cursor(future, "goals", label="Goal")
    late = encode_snapshot_cursor(
        "goals", now - timedelta(hours=2), now - timedelta(hours=1), uuid4()
    )
    with pytest.raises(ValidationFailedError):
        decode_snapshot_cursor(late, "goals", label="Goal")


def test_bounded_page_counts_json_array_bytes_and_digests_match() -> None:
    items = [_Dto(value="a"), _Dto(value="b")]
    encoded, size = serialize_bounded_page(items, label="Task")
    assert size == len(
        json.dumps([json.loads(raw) for raw in encoded], separators=(",", ":")).encode()
    )
    assert (
        content_digest(encoded[0]) == hashlib.sha256(encoded[0]).hexdigest() == dto_digest(items[0])
    )
    assert serialize_bounded_page([], label="Task") == ([], 2)


def test_bounded_page_rejects_oversize_with_413() -> None:
    with pytest.raises(PayloadTooLargeError) as caught:
        serialize_bounded_page([_Dto(value="x" * 50)], label="Goal", max_bytes=10)
    assert caught.value.status_code == 413
    assert caught.value.detail == "Goal export page exceeds its byte bound"
