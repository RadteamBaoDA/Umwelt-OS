"""Validation and mapping helpers for the Notion and Slack collectors, plus the bounded response budget."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from modules.connectors.providers import rest
from modules.connectors.providers import workspace_apps as wa

COLLECTED = datetime(2026, 2, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "token,ids,provider,error",
    [
        ("", ("C1",), "slack", "slack_credential_invalid"),
        ("t\n", ("C1",), "slack", "slack_credential_invalid"),
        ("t" * 9000, ("C1",), "notion", "notion_credential_invalid"),
        ("t", (), "notion", "notion_scope_invalid"),
        ("t", ("a", "a"), "notion", "notion_scope_invalid"),
        ("t", ("bad id",), "notion", "notion_scope_invalid"),
        ("t", tuple(f"p{i}" for i in range(101)), "notion", "notion_scope_invalid"),
        ("t", ["C1"], "notion", "notion_scope_invalid"),
        ("t", ("lowercase",), "slack", "slack_scope_invalid"),
        ("t", ("D12345",), "slack", "slack_scope_invalid"),
    ],
)
def test_validate_inputs_rejects_unsafe_credentials_and_scopes(
    token: str, ids: Any, provider: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        wa._validate_inputs(token, ids, provider)


def test_validate_inputs_accepts_selected_resources() -> None:
    wa._validate_inputs("secret", ("C123", "G456"), "slack")
    wa._validate_inputs("secret", ("page-1", "page_2"), "notion")


def test_aware_utc_requires_timezone() -> None:
    with pytest.raises(ValueError, match="slack_time_invalid"):
        wa._aware_utc(datetime(2026, 1, 1), "slack")
    assert wa._aware_utc(datetime(2026, 1, 1, 2, tzinfo=UTC), "slack").hour == 2


def test_notion_plain_text_joins_literals_and_ignores_non_text() -> None:
    assert (
        wa._notion_plain_text([{"plain_text": "a"}, {"plain_text": 3}, "x", {"plain_text": "b"}])
        == "ab"
    )
    assert wa._notion_plain_text("nope") == ""
    with pytest.raises(ValueError, match="notion_text_invalid"):
        wa._notion_plain_text([{"plain_text": "a\x00"}])
    with pytest.raises(rest.CollectionIncomplete):
        wa._notion_plain_text([{"plain_text": "x" * (wa.MAX_TEXT_CHARS + 1)}])


def test_notion_title_reads_the_declared_title_property() -> None:
    page = {
        "properties": {"Tags": {"multi_select": []}, "Name": {"title": [{"plain_text": "Roadmap"}]}}
    }
    assert wa._notion_title(page) == "Roadmap"
    assert wa._notion_title({"properties": "x"}) == ""
    assert wa._notion_title({"properties": {"A": {"rich": 1}}}) == ""


def test_notion_block_text_prefers_rich_text_then_title_caption_cells() -> None:
    assert (
        wa._notion_block_text(
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "hi"}]}}
        )
        == "hi"
    )
    assert wa._notion_block_text({"type": "child_page", "child_page": {"title": "Sub"}}) == "Sub"
    assert (
        wa._notion_block_text({"type": "image", "image": {"caption": [{"plain_text": "pic"}]}})
        == "pic"
    )
    row = {
        "type": "table_row",
        "table_row": {"cells": [[{"plain_text": "a"}], [{"plain_text": "b"}]]},
    }
    assert wa._notion_block_text(row) == "a | b"
    assert wa._notion_block_text({"type": "divider", "divider": {}}) == ""
    assert wa._notion_block_text({"type": 5}) == ""
    with pytest.raises(ValueError, match="notion_block_invalid"):
        wa._notion_block_text({"type": "child_page", "child_page": {"title": "a\x00"}})
    with pytest.raises(rest.CollectionIncomplete):
        wa._notion_block_text(
            {"type": "child_page", "child_page": {"title": "x" * (wa.MAX_TEXT_CHARS + 1)}}
        )


def test_slack_timestamp_is_exact_decimal() -> None:
    instant, raw = wa._slack_timestamp("1700000000.123456")
    assert raw == "1700000000.123456"
    assert instant == datetime.fromtimestamp(1_700_000_000, UTC).replace(microsecond=123456)


@pytest.mark.parametrize("value", [None, 5, "abc", "0", "1.", "-1", "1" * 20, "1.1234567890"])
def test_slack_timestamp_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match="slack_timestamp_invalid"):
        wa._slack_timestamp(value)


def test_slack_oldest_formats_without_float_loss() -> None:
    assert wa._slack_oldest(datetime(1970, 1, 2, 0, 0, 1, 5, tzinfo=UTC)) == "86401.000005"
    with pytest.raises(ValueError, match="slack_time_invalid"):
        wa._slack_oldest(datetime(1969, 12, 31, tzinfo=UTC))


def _channel(**over: Any) -> dict[str, Any]:
    channel: dict[str, Any] = {
        "id": "C1",
        "is_im": False,
        "is_mpim": False,
        "is_private": False,
        "is_channel": True,
        "is_group": False,
        "is_member": True,
        "is_archived": False,
    }
    channel.update(over)
    return {"ok": True, "channel": channel}


def test_require_slack_channel_accepts_member_public_and_private() -> None:
    wa._require_slack_channel(_channel(), "C1")
    wa._require_slack_channel(_channel(is_private=True, is_channel=False, is_group=True), "C1")


@pytest.mark.parametrize(
    "info,error",
    [
        ({"ok": False}, "slack_channel_invalid"),
        ({"ok": True, "channel": "x"}, "slack_channel_invalid"),
        (_channel(id="C2"), "slack_channel_invalid"),
        (_channel(is_im=True), "slack_channel_unavailable"),
        (_channel(is_mpim=True), "slack_channel_unavailable"),
        (_channel(is_member=False), "slack_channel_unavailable"),
        (_channel(is_archived=True), "slack_channel_unavailable"),
        (_channel(is_private=None), "slack_channel_unavailable"),
        (_channel(is_private=False, is_channel=False), "slack_channel_unavailable"),
    ],
)
def test_require_slack_channel_rejects_unsupported_channels(
    info: dict[str, Any], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        wa._require_slack_channel(info, "C1")


def _record(message: object, seen: set[str] | None = None, count: int = 0):
    return wa._slack_message_record(
        message, "C1", COLLECTED, count, seen if seen is not None else set()
    )


def test_slack_message_record_maps_published_and_edited_messages() -> None:
    plain = _record({"ts": "1700000000.000100", "text": "hi", "user": "U1"})
    assert plain is not None
    assert plain.provider_id == "C1/1700000000.000100"
    assert plain.version == "1700000000.000100"
    assert plain.metadata["provider_record"]["timestamp_basis"] == "provider_published"
    edited = _record(
        {"ts": "1700000000.000100", "text": "hi", "edited": {"ts": "1700000100.000000"}}
    )
    assert edited is not None
    assert edited.version == "1700000100.000000"
    assert edited.metadata["provider_record"]["timestamp_basis"] == "provider_modified"


def test_slack_message_record_skips_thread_replies_and_duplicates() -> None:
    assert _record({"ts": "1700000001.000000", "thread_ts": "1700000000.000000"}) is None
    root = {"ts": "1700000000.000000", "thread_ts": "1700000000.000000", "text": "root"}
    assert _record(root) is not None
    seen = {"C1/1700000000.000000"}
    assert _record({"ts": "1700000000.000000"}, seen) is None


@pytest.mark.parametrize(
    "message",
    [
        "x",
        {"ts": "1700000000.0", "thread_ts": 5},
        {"ts": "1700000000.0", "channel": "C9"},
        {"ts": "1700000000.0", "text": 3},
        {"ts": "1700000000.0", "text": "a\x00"},
        {"ts": "1700000000.0", "edited": "x"},
        {"ts": "1700000000.0", "edited": {}},
        {"ts": "1700000000.0", "user": "u" * 300},
    ],
)
def test_slack_message_record_rejects_malformed_messages(message: object) -> None:
    with pytest.raises(ValueError):
        _record(message)


def test_slack_message_record_enforces_record_and_text_caps() -> None:
    with pytest.raises(rest.CollectionIncomplete):
        _record({"ts": "1700000000.0"}, count=wa.MAX_RECORDS)
    with pytest.raises(rest.CollectionIncomplete):
        _record({"ts": "1700000000.0", "text": "x" * (wa.MAX_TEXT_CHARS + 1)})


def test_slack_next_cursor_rules() -> None:
    seen: set[str] = set()
    assert wa._slack_next_cursor({"response_metadata": {"next_cursor": "c1"}}, True, seen) == "c1"
    assert seen == {"c1"}
    assert wa._slack_next_cursor({}, False, seen) is None
    assert wa._slack_next_cursor({"response_metadata": {"next_cursor": ""}}, False, seen) is None
    for payload, more in (
        ({"response_metadata": {"next_cursor": "c1"}}, True),
        ({"response_metadata": {"next_cursor": "x" * 5000}}, True),
        ({}, True),
        ({"response_metadata": {"next_cursor": "more"}}, False),
        ({"response_metadata": "x"}, False),
    ):
        with pytest.raises(ValueError, match="slack_cursor_invalid"):
            wa._slack_next_cursor(payload, more, seen)


def _budget(provider: str, body: bytes | None, **over: Any) -> wa._Budget:
    async def fetch(url: str, **kwargs: Any) -> Any:
        fetch.kwargs = kwargs  # type: ignore[attr-defined]
        return SimpleNamespace(body=body)

    budget = wa._Budget(token="tok", fetch_bytes=fetch, provider=provider)
    for key, value in over.items():
        setattr(budget, key, value)
    return budget


async def test_budget_get_sends_bearer_and_notion_version_headers() -> None:
    budget = _budget("notion", json.dumps({"object": "list"}).encode())
    assert await budget.get("https://api.notion.com/v1/x") == {"object": "list"}
    headers = budget.fetch_bytes.kwargs["headers"]  # type: ignore[attr-defined]
    assert (
        headers["Authorization"] == "Bearer tok"
        and headers["Notion-Version"] == wa.NOTION_API_VERSION
    )
    slack = _budget("slack", b'{"ok": true}')
    await slack.get("https://slack.com/api/x")
    assert "Notion-Version" not in slack.fetch_bytes.kwargs["headers"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("body", [None, b"not json", b"[]", b"\xff"])
async def test_budget_get_rejects_invalid_responses(body: bytes | None) -> None:
    with pytest.raises(ValueError, match="slack_response_invalid"):
        await _budget("slack", body).get("u")


async def test_budget_get_maps_provider_errors_and_rate_limits() -> None:
    with pytest.raises(ValueError, match="slack_provider_rejected"):
        await _budget("slack", b'{"ok": false, "error": "invalid_auth"}').get("u")
    with pytest.raises(wa.ProviderRateLimited):
        await _budget("slack", b'{"ok": false, "error": "ratelimited"}').get("u")
    with pytest.raises(ValueError, match="notion_provider_rejected"):
        await _budget("notion", b'{"object": "error", "code": "unauthorized"}').get("u")
    with pytest.raises(wa.ProviderRateLimited):
        await _budget("notion", b'{"object": "error", "code": "rate_limited"}').get("u")


async def test_budget_get_enforces_request_and_byte_caps() -> None:
    with pytest.raises(rest.CollectionIncomplete):
        await _budget("slack", b"{}", request_count=wa.MAX_REQUESTS).get("u")
    with pytest.raises(rest.CollectionIncomplete):
        await _budget("slack", b"{}", transport_bytes=wa.MAX_TOTAL_BYTES).get("u")
