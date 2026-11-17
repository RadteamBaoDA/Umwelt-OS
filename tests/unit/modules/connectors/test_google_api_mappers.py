"""Pure mapper/validator tests for the Google Gmail, Calendar and Drive collectors."""

import base64
import json
from datetime import UTC, datetime

import pytest

from modules.connectors.providers import google_api as g
from modules.connectors.providers import rest

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_json_decodes_decimals_and_rejects_garbage() -> None:
    from decimal import Decimal

    assert g._json(b'{"a": 1.5}') == {"a": Decimal("1.5")}
    for bad in (b"{", b"\xff\xfe"):
        with pytest.raises(g.GoogleProviderError, match="response_not_json"):
            g._json(bad)


@pytest.mark.parametrize("page,expected", [({}, None), ({"nextPageToken": "t"}, "t")])
def test_next_page_valid(page: dict[str, object], expected: str | None) -> None:
    assert g._next_page(page) == expected


@pytest.mark.parametrize("token", ["", None, 5])
def test_next_page_rejects_malformed_token(token: object) -> None:
    with pytest.raises(g.GoogleProviderError, match="page_token_invalid"):
        g._next_page({"nextPageToken": token})


def test_text_truncates_and_rejects_non_strings() -> None:
    assert g._text("abcdef", 3) == "abc"
    with pytest.raises(g.GoogleProviderError):
        g._text(None)


def test_utc_normalizes_offsets_and_enforces_awareness() -> None:
    assert g._utc("2026-01-01T02:00:00+02:00") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(g.GoogleProviderError, match="timestamp_invalid"):
        g._utc("2026-01-01T00:00:00")
    with pytest.raises(g.GoogleProviderError, match="timestamp_invalid"):
        g._utc("nope")
    with pytest.raises(g.GoogleProviderError, match="timestamp_missing"):
        g._utc("")
    assert g._utc(None, fallback=NOW) == NOW


def test_cursor_round_trip_and_provider_isolation() -> None:
    raw = g._cursor_json({"v": 2, "provider": "gmail", "phase": "history"})
    assert g._read_cursor(raw, "gmail") == {"v": 2, "provider": "gmail", "phase": "history"}
    assert g._read_cursor(None, "gmail") is None
    with pytest.raises(g.GoogleProviderError, match="cursor_invalid"):
        g._read_cursor(raw, "drive")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"v": 3, "provider": "gmail"}',
        '{"v": true, "provider": "gmail"}',
        "x" * 20000,
    ],
)
def test_read_cursor_rejects_invalid_shapes(raw: str) -> None:
    with pytest.raises(g.GoogleProviderError, match="cursor_invalid"):
        g._read_cursor(raw, "gmail")


def test_cursor_json_enforces_byte_bound() -> None:
    with pytest.raises(rest.CollectionIncomplete):
        g._cursor_json({"blob": "x" * (g.MAX_CURSOR_BYTES + 1)})


def test_gmail_plain_text_collects_inline_text_only() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("first")}},
            {"mimeType": "text/plain", "filename": "a.txt", "body": {"data": _b64("attached")}},
            {"mimeType": "text/plain", "body": {"data": _b64("att"), "attachmentId": "x"}},
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/plain", "body": {"data": _b64("second")}}],
            },
            {"mimeType": "text/html", "body": {"data": _b64("<b>no</b>")}},
            "garbage",
        ],
    }
    assert g._gmail_plain_text(payload) == "first\nsecond"


def test_gmail_plain_text_rejects_bad_body_and_oversized_trees() -> None:
    with pytest.raises(g.GoogleProviderError, match="gmail_body_invalid"):
        g._gmail_plain_text({"mimeType": "text/plain", "body": {"data": "////"}})
    many = {"parts": [{} for _ in range(1001)]}
    with pytest.raises(rest.CollectionIncomplete):
        g._gmail_plain_text(many)
    big = {"mimeType": "text/plain", "body": {"data": _b64("x" * 1_000_001)}}
    with pytest.raises(rest.CollectionIncomplete):
        g._gmail_plain_text(big)


def _message(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": "m1",
        "threadId": "t1",
        "historyId": "42",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX", 5],
        "snippet": "snip",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "b@example.com"},
                {"name": "Date", "value": "Tue"},
                "junk",
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64("body text")},
        },
    }
    message.update(overrides)
    return message


def test_gmail_message_record_maps_headers_body_and_provenance() -> None:
    record = g._gmail_message_record(_message(), NOW)
    assert record.provider_id == "m1"
    assert record.content == "Hello\n\nbody text"
    assert record.version == "42"
    assert record.observed_at == datetime.fromtimestamp(1700000000, tz=UTC)
    assert record.metadata["label_ids"] == ["INBOX"]
    assert record.metadata["from"] == "a@example.com"
    assert record.metadata["provider_record"]["provider"] == "google_mail"
    assert record.metadata["provider_record"]["timestamp_basis"] == "provider_published"


def test_gmail_message_record_falls_back_to_snippet_without_subject() -> None:
    message = _message(payload={"headers": []})
    assert g._gmail_message_record(message, NOW).content == "snip"


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"id": ""}, "gmail_message_identity_invalid"),
        ({"threadId": None}, "gmail_message_identity_invalid"),
        ({"id": "x" * 513}, "gmail_message_identity_invalid"),
        ({"internalDate": "abc"}, "gmail_timestamp_invalid"),
        ({"internalDate": True}, "gmail_timestamp_invalid"),
        ({"internalDate": "9" * 30}, "gmail_timestamp_invalid"),
        ({"historyId": "x"}, "gmail_history_id_invalid"),
    ],
)
def test_gmail_message_record_rejects_invalid_fields(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(g.GoogleProviderError, match=error):
        g._gmail_message_record(_message(**overrides), NOW)


def test_gmail_message_record_rejects_non_object() -> None:
    with pytest.raises(g.GoogleProviderError, match="gmail_message_invalid"):
        g._gmail_message_record("nope", NOW)


def test_history_state_and_message_ids_dedupe() -> None:
    state = g._history_state(["INBOX"], "9")
    assert (
        state["phase"] == "history"
        and state["history_offset"] == 0
        and state["labels"] == ["INBOX"]
    )
    page = {
        "history": [
            {"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]},
            {
                "labelsAdded": [{"message": {"id": "a"}}, {"message": {"id": 7}}, "junk"],
                "messagesAdded": "bad",
            },
            "junk",
        ]
    }
    assert g._history_message_ids(page) == ["a", "b"]
    assert g._history_message_ids({}) == []
    with pytest.raises(g.GoogleProviderError, match="gmail_history_invalid"):
        g._history_message_ids({"history": {}})


def test_calendar_event_record_timed_event_with_provenance() -> None:
    event = {
        "id": "e1",
        "status": "tentative",
        "summary": "Standup",
        "description": "Daily",
        "start": {"dateTime": "2026-03-01T10:00:00+00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-01T10:30:00+00:00"},
        "updated": "2026-02-01T00:00:00+00:00",
        "recurrence": ["RRULE:FREQ=DAILY"],
        "htmlLink": "https://cal.example/e1",
    }
    record = g._calendar_event_record("cal", event, NOW)
    assert record.provider_id == "cal/e1"
    assert record.content == "Standup\n\nDaily"
    assert record.version == "2026-02-01T00:00:00+00:00"
    assert record.observed_at == datetime(2026, 3, 1, 10, tzinfo=UTC)
    provenance = record.metadata["provider_record"]
    assert provenance["timestamp_basis"] == "provider_modified"
    assert provenance["source_fields"]["start_timezone"] == "UTC"
    assert provenance["source_fields"]["html_link"] == "https://cal.example/e1"


def test_calendar_all_day_event_is_midnight_utc() -> None:
    record = g._calendar_event_record("cal", {"id": "e", "start": {"date": "2026-03-01"}}, NOW)
    assert record.observed_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert record.metadata["provider_record"]["timestamp_basis"] == "collection"


def test_calendar_cancelled_event_without_start_uses_collection_time() -> None:
    record = g._calendar_event_record("cal", {"id": "e", "status": "cancelled"}, NOW)
    assert record.observed_at == NOW
    with pytest.raises(g.GoogleProviderError, match="timestamp_missing"):
        g._calendar_event_record("cal", {"id": "e"}, NOW)


@pytest.mark.parametrize(
    "event,error",
    [
        ("x", "calendar_event_invalid"),
        ({"id": ""}, "calendar_event_identity_invalid"),
        ({"id": "e", "status": "weird"}, "calendar_event_identity_invalid"),
        ({"id": "e" * 600}, "calendar_event_identity_invalid"),
        ({"id": "e", "start": {"date": "2026-13-45"}}, "calendar_event_time_invalid"),
        (
            {"id": "e", "start": {"date": "2026-01-01"}, "recurrence": [1]},
            "calendar_recurrence_invalid",
        ),
    ],
)
def test_calendar_event_record_rejects_invalid_events(event: object, error: str) -> None:
    with pytest.raises(g.GoogleProviderError, match=error):
        g._calendar_event_record("cal", event, NOW)


def test_calendar_source_fields_omit_missing_values_and_cap_recurrence() -> None:
    fields = g._calendar_source_fields(
        "c", "e", "confirmed", [f"R{i}" for i in range(40)], None, {}, {}, {}
    )
    assert fields == {
        "calendar_id": "c",
        "event_id": "e",
        "status": "confirmed",
        "recurrence": [f"R{i}" for i in range(32)],
    }


def test_drive_file_record_plain_text_and_modified_basis() -> None:
    file = {
        "id": "f1",
        "name": "n.txt",
        "modifiedTime": "2026-02-01T00:00:00Z",
        "webViewLink": "https://d/1",
        "md5Checksum": "abc",
    }
    record = g._drive_file_record(file, b"hello", "text/plain", NOW)
    assert record.content == "hello"
    assert record.version == "2026-02-01T00:00:00Z"
    assert record.metadata["provider_record"]["timestamp_basis"] == "provider_modified"
    assert record.metadata["md5_checksum"] == "abc"


def test_drive_file_record_without_modified_time_uses_collection_basis() -> None:
    record = g._drive_file_record({"id": "f1", "version": 3}, b"x", "text/plain", NOW)
    assert record.observed_at == NOW
    assert record.version == "3"
    assert record.metadata["provider_record"]["timestamp_basis"] == "collection"


@pytest.mark.parametrize(
    "file,content,mime,error",
    [
        (None, b"x", "text/plain", "drive_file_invalid"),
        ({"id": ""}, b"x", "text/plain", "drive_file_invalid"),
        ({"id": "x" * 513}, b"x", "text/plain", "drive_file_identity_invalid"),
        ({"id": "f"}, b"\xff", "text/plain", "drive_text_not_utf8"),
        ({"id": "f"}, b"{bad", "application/json", "drive_json_invalid"),
    ],
)
def test_drive_file_record_rejects_invalid_input(
    file: object, content: bytes, mime: str, error: str
) -> None:
    with pytest.raises(g.GoogleProviderError, match=error):
        g._drive_file_record(file, content, mime, NOW)


def test_drive_file_record_accepts_valid_json_and_rejects_oversized_text() -> None:
    assert (
        g._drive_file_record(
            {"id": "f"}, json.dumps({"a": 1}).encode(), "application/json", NOW
        ).content
        == '{"a": 1}'
    )
    with pytest.raises(rest.CollectionIncomplete):
        g._drive_file_record({"id": "f"}, b"x" * 1_000_001, "text/plain", NOW)


def test_drive_initial_state_new_migrated_and_existing() -> None:
    fresh = g._drive_initial_state(None, "h")
    assert fresh["phase"] == "snapshot" and fresh["scope_hash"] == "h"
    migrated = g._drive_initial_state({"v": 1, "provider": "drive", "page_token": "tok"}, "h")
    assert migrated["phase"] == "changes" and migrated["change_token"] == "tok"
    existing = {"phase": "snapshot", "x": 1}
    assert g._drive_initial_state(existing, "h") is existing
    with pytest.raises(g.GoogleProviderError, match="drive_cursor_invalid"):
        g._drive_initial_state({"v": 1}, "h")


def test_append_record_enforces_record_and_batch_caps() -> None:
    record = g._drive_file_record({"id": "f"}, b"x", "text/plain", NOW)
    records: list = []
    total = g._append_record(records, record, 0)
    assert records == [record] and total > 0
    with pytest.raises(rest.CollectionIncomplete):
        g._append_record(records, record, g.MAX_BATCH_BYTES)
    with pytest.raises(rest.CollectionIncomplete):
        g._append_record([record] * g.MAX_RECORDS, record, 0)
