"""Bounded read-only Notion and Slack collectors for selected workspace resources.

The executor must inject its DNS-pinned ``fetch_bytes`` callback; that callback is also
responsible for rechecking the collection send fence immediately before every request.
"""

# ValueError is the executor's stable classification for invalid provider payloads and scopes.
# ruff: noqa: TRY004

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from modules.connectors.providers import rest
from modules.connectors.providers.rest import Fetched
from modules.connectors.public import ProviderCollectionPage, ProviderRateLimited
from modules.ingestion.schemas import IngestionRecord

NOTION_API_VERSION = "2026-03-11"
NOTION_ORIGIN = "https://api.notion.com/v1"
SLACK_ORIGIN = "https://slack.com/api"
MAX_PAGES = 10
MAX_REQUESTS = 500
MAX_RECORDS = 500
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_NESTING_DEPTH = 5
MAX_BLOCKS = 5000
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{1,80}$")
_SLACK_TS = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,9})?$")
FetchBytes = Callable[..., Awaitable[Fetched]]


@dataclass(frozen=True, slots=True)
class NotionScope:
    """Selected Notion page IDs granted to the internal integration."""

    page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SlackScope:
    """Selected Slack channel IDs and optional inclusive history start instant."""

    channel_ids: tuple[str, ...]
    oldest: datetime | None = None


@dataclass
class _Budget:
    """Count fixed-origin response bytes, request count and Notion blocks per adapter call."""

    token: str
    fetch_bytes: FetchBytes
    provider: str
    transport_bytes: int = 0
    block_count: int = 0
    request_count: int = 0

    async def get(self, url: str) -> dict[str, Any]:
        """Fetch and decode one bounded provider JSON response without exposing credentials."""
        self.request_count += 1
        if self.request_count > MAX_REQUESTS:
            raise rest.CollectionIncomplete
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if self.provider == "notion":
            headers["Notion-Version"] = NOTION_API_VERSION
        fetched = await self.fetch_bytes(url, headers=headers, max_bytes=MAX_PAGE_BYTES)
        body = fetched.body
        if body is None:
            raise ValueError(f"{self.provider}_response_invalid")
        self.transport_bytes += len(body)
        if self.transport_bytes > MAX_TOTAL_BYTES:
            raise rest.CollectionIncomplete
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{self.provider}_response_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{self.provider}_response_invalid")
        error = payload.get("code") or payload.get("error")
        if self.provider == "slack" and payload.get("ok") is False:
            if isinstance(error, str) and error in {"ratelimited", "rate_limited"}:
                raise ProviderRateLimited(datetime.now(UTC) + timedelta(seconds=60))
            raise ValueError("slack_provider_rejected")
        if self.provider == "notion" and payload.get("object") == "error":
            if isinstance(error, str) and error in {"rate_limited", "rate_limited_error"}:
                raise ProviderRateLimited(datetime.now(UTC) + timedelta(seconds=60))
            raise ValueError("notion_provider_rejected")
        return payload


def _validate_inputs(token: str, identifiers: tuple[str, ...], provider: str) -> None:
    """Reject empty credentials and malformed, duplicate, or excessive owner-selected scopes."""
    if not isinstance(token, str) or not token or len(token) > 8192 or any(char in token for char in "\r\n"):
        raise ValueError(f"{provider}_credential_invalid")
    if (not isinstance(identifiers, tuple) or not identifiers or len(identifiers) > 100
            or any(not isinstance(value, str) or not _RESOURCE_ID.fullmatch(value) for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError(f"{provider}_scope_invalid")
    if provider == "slack" and any(not _SLACK_CHANNEL_ID.fullmatch(value) for value in identifiers):
        raise ValueError("slack_scope_invalid")


def _aware_utc(value: datetime, provider: str) -> datetime:
    """Require a timezone-aware clock before provider timestamps or windows are constructed."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{provider}_time_invalid")
    return value.astimezone(UTC)


def _notion_plain_text(value: object) -> str:
    """Extract bounded Notion rich-text literals without downloading linked/file payloads."""
    if not isinstance(value, list):
        return ""
    parts = [item["plain_text"] for item in value
             if isinstance(item, dict) and isinstance(item.get("plain_text"), str)]
    text = "".join(parts)
    if len(text) > MAX_TEXT_CHARS:
        raise rest.CollectionIncomplete
    if "\x00" in text:
        raise ValueError("notion_text_invalid")
    return text


def _notion_title(page: Mapping[str, Any]) -> str:
    """Read the provider-declared title property from a page without guessing property names."""
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return ""
    for value in properties.values():
        if isinstance(value, dict) and isinstance(value.get("title"), list):
            return _notion_plain_text(value["title"])
    return ""


def _notion_block_text(block: Mapping[str, Any]) -> str:
    """Extract only textual block fields and leave files, embeds and rich media unfetched."""
    block_type = block.get("type")
    data = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(data, dict):
        return ""
    rich_text = _notion_plain_text(data.get("rich_text"))
    if rich_text:
        return rich_text
    for key in ("title", "caption"):
        value = data.get(key)
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("notion_block_invalid")
            if len(value) > MAX_TEXT_CHARS:
                raise rest.CollectionIncomplete
            return value
        text = _notion_plain_text(value)
        if text:
            return text
    cells = data.get("cells")
    if isinstance(cells, list):
        return " | ".join(_notion_plain_text(cell) for cell in cells)
    return ""


async def _notion_children(
    block_id: str,
    budget: _Budget,
    visited: set[str],
    depth: int,
) -> list[str]:
    """Collect one selected page/block subtree with bounded pagination, depth and deduplication."""
    if depth > MAX_NESTING_DEPTH or block_id in visited:
        raise rest.CollectionIncomplete
    visited.add(block_id)
    lines: list[str] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(MAX_PAGES):
        query: dict[str, str] = {"page_size": "100"}
        if cursor:
            query["start_cursor"] = cursor
        url = f"{NOTION_ORIGIN}/blocks/{quote(block_id, safe='')}/children?{urlencode(query)}"
        payload = await budget.get(url)
        results = payload.get("results")
        more = payload.get("has_more")
        if not isinstance(results, list) or not isinstance(more, bool):
            raise ValueError("notion_children_invalid")
        for block in results:
            budget.block_count += 1
            if budget.block_count > MAX_BLOCKS:
                raise rest.CollectionIncomplete
            if (not isinstance(block, dict) or not isinstance(block.get("id"), str)
                    or not _RESOURCE_ID.fullmatch(block["id"])
                    or not isinstance(block.get("has_children", False), bool)):
                raise ValueError("notion_block_invalid")
            text = _notion_block_text(block)
            if text:
                lines.append(text)
            if block.get("has_children") is True and block.get("type") != "child_database":
                lines.extend(await _notion_children(block["id"], budget, visited, depth + 1))
        if not more:
            if payload.get("next_cursor") not in (None, ""):
                raise ValueError("notion_cursor_invalid")
            return lines
        next_cursor = payload.get("next_cursor")
        if (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096
                or next_cursor in seen_cursors):
            raise ValueError("notion_cursor_invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise rest.CollectionIncomplete


async def collect_notion_pages(
    token: str,
    scope: NotionScope,
    *,
    fetch_bytes: FetchBytes,
    collected_at: datetime | None = None,
) -> ProviderCollectionPage:
    """Read selected Notion pages and text blocks using API version 2026-03-11.

    Each page yields one versioned record. Pagination, recursion, response bytes, block count,
    records and batch bytes are bounded; cap exhaustion raises ``CollectionIncomplete`` so an
    incomplete snapshot cannot be accepted. The injected transport must be executor-bounded and
    rerun the source/credential send fence for every physical request. Only page IDs are queried;
    file contents, attachments, databases, and workspace-wide search are outside this adapter.
    """
    if not isinstance(scope, NotionScope):
        raise ValueError("notion_scope_invalid")
    _validate_inputs(token, scope.page_ids, "notion")
    collected = _aware_utc(collected_at or datetime.now(UTC), "notion")
    budget = _Budget(token, fetch_bytes, "notion")
    records: list[IngestionRecord] = []
    batch_bytes = 0
    for page_id in scope.page_ids:
        if len(records) >= MAX_RECORDS:
            raise rest.CollectionIncomplete
        page = await budget.get(f"{NOTION_ORIGIN}/pages/{quote(page_id, safe='')}")
        if page.get("object") != "page" or page.get("id") != page_id:
            raise ValueError("notion_page_invalid")
        edited_raw = page.get("last_edited_time")
        if not isinstance(edited_raw, str):
            raise ValueError("notion_timestamp_invalid")
        try:
            edited = _aware_utc(datetime.fromisoformat(edited_raw), "notion")
        except ValueError as exc:
            raise ValueError("notion_timestamp_invalid") from exc
        title = _notion_title(page)
        body = await _notion_children(page_id, budget, set(), 0)
        content = "\n".join(([title] if title else []) + body)
        if len(content) > MAX_TEXT_CHARS:
            raise rest.CollectionIncomplete
        url = page.get("url")
        parsed_url = urlsplit(url) if isinstance(url, str) and len(url) <= 2048 else None
        canonical_url = (
            url if isinstance(url, str) and parsed_url is not None
            and parsed_url.scheme == "https" and parsed_url.hostname
            and parsed_url.username is None and parsed_url.password is None and "\x00" not in url else None
        )
        provider_record = {
            "provider": "notion",
            "identity": page_id,
            "provider_version": edited_raw,
            "timestamp_basis": "provider_modified",
            "coverage": "returned_snapshot",
            "content_truncated": False,
            "provider_modified_at": edited.isoformat(),
            "source_fields": {"title": title[:500], "canonical_url": canonical_url}
            if canonical_url else {"title": title[:500]},
        }
        metadata = {
            "provider_record": provider_record,
            "title": title[:1000],
        }
        if canonical_url:
            metadata["canonical_url"] = canonical_url
        record = IngestionRecord(
            provider_id=page_id,
            content=content,
            observed_at=edited,
            version=edited_raw[:255],
            metadata=metadata,
            collected_at=collected,
        )
        record_bytes = len(record.model_dump_json().encode("utf-8"))
        if batch_bytes + record_bytes > MAX_BATCH_BYTES:
            raise rest.CollectionIncomplete
        records.append(record)
        batch_bytes += record_bytes
    return ProviderCollectionPage(records=tuple(records), coverage="returned_snapshot")


def _slack_timestamp(value: object) -> tuple[datetime, str]:
    """Parse Slack's decimal Unix timestamp without floating-point rounding or fallback time."""
    if not isinstance(value, str) or not _SLACK_TS.fullmatch(value):
        raise ValueError("slack_timestamp_invalid")
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0:
            raise InvalidOperation
        seconds = int(number)
        microseconds = int((number - Decimal(seconds)) * 1_000_000)
        instant = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=microseconds)
    except (InvalidOperation, OverflowError, OSError, ValueError) as exc:
        raise ValueError("slack_timestamp_invalid") from exc
    return instant, value


def _slack_oldest(value: datetime) -> str:
    """Format a UTC instant as Slack's decimal Unix time without float conversion."""
    instant = _aware_utc(value, "slack")
    delta = instant - datetime(1970, 1, 1, tzinfo=UTC)
    if delta.days < 0:
        raise ValueError("slack_time_invalid")
    seconds = delta.days * 86_400 + delta.seconds
    return f"{seconds}.{delta.microseconds:06d}"


async def collect_slack_channel_messages(
    token: str,
    scope: SlackScope,
    *,
    fetch_bytes: FetchBytes,
    collected_at: datetime | None = None,
) -> ProviderCollectionPage:
    """Collect top-level messages from selected Slack channels through paginated history.

    The initial window begins 30 days before collection unless ``scope.oldest`` is supplied.
    Each selected conversation is verified through ``conversations.info`` before history is read;
    only active public/private channels where the bot is a member are accepted. Replies are
    excluded even if returned by the API. Slack HTTP 429 is propagated from the bounded transport
    as ``ProviderRateLimited`` with its safe Retry-After deadline; API-body rate-limit errors use
    a conservative 60-second deadline. Pagination/schema/byte overflow raises
    ``CollectionIncomplete``. The injected fetch callback must fence every send.
    """
    if not isinstance(scope, SlackScope):
        raise ValueError("slack_scope_invalid")
    _validate_inputs(token, scope.channel_ids, "slack")
    collected = _aware_utc(collected_at or datetime.now(UTC), "slack")
    oldest = _aware_utc(scope.oldest, "slack") if scope.oldest is not None else collected - timedelta(days=30)
    if oldest > collected:
        raise ValueError("slack_time_invalid")
    budget = _Budget(token, fetch_bytes, "slack")
    records: list[IngestionRecord] = []
    batch_bytes = 0
    seen_messages: set[str] = set()
    for channel_id in scope.channel_ids:
        info = await budget.get(
            f"{SLACK_ORIGIN}/conversations.info?{urlencode({'channel': channel_id})}"
        )
        channel = info.get("channel")
        if info.get("ok") is not True or not isinstance(channel, dict) or channel.get("id") != channel_id:
            raise ValueError("slack_channel_invalid")
        is_im, is_mpim = channel.get("is_im"), channel.get("is_mpim")
        is_private = channel.get("is_private")
        is_channel, is_group = channel.get("is_channel"), channel.get("is_group")
        is_supported_channel = (
            (is_private is False and is_channel is True)
            or (is_private is True and (is_channel is True or is_group is True))
        )
        if (is_im is not False or is_mpim is not False or not isinstance(is_private, bool)
                or not is_supported_channel
                or channel.get("is_member") is not True or channel.get("is_archived") is not False):
            raise ValueError("slack_channel_unavailable")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_PAGES):
            params = {
                "channel": channel_id, "oldest": _slack_oldest(oldest),
                "inclusive": "true", "limit": "200",
            }
            if cursor:
                params["cursor"] = cursor
            payload = await budget.get(f"{SLACK_ORIGIN}/conversations.history?{urlencode(params)}")
            messages, more = payload.get("messages"), payload.get("has_more")
            if not isinstance(messages, list) or not isinstance(more, bool):
                raise ValueError("slack_history_invalid")
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("slack_message_invalid")
                timestamp_value = message.get("ts")
                observed_at, timestamp = _slack_timestamp(timestamp_value)
                thread_ts = message.get("thread_ts")
                if thread_ts is not None and not isinstance(thread_ts, str):
                    raise ValueError("slack_message_invalid")
                if isinstance(thread_ts, str):
                    _slack_timestamp(thread_ts)
                if isinstance(thread_ts, str) and thread_ts != timestamp:
                    continue
                if message.get("channel", channel_id) != channel_id:
                    raise ValueError("slack_message_invalid")
                if len(records) >= MAX_RECORDS:
                    raise rest.CollectionIncomplete
                text = message.get("text", "")
                if not isinstance(text, str) or "\x00" in text:
                    raise ValueError("slack_message_invalid")
                if len(text) > MAX_TEXT_CHARS:
                    raise rest.CollectionIncomplete
                edited = message.get("edited")
                if edited is not None and not isinstance(edited, dict):
                    raise ValueError("slack_message_invalid")
                edited_ts = edited.get("ts") if isinstance(edited, dict) else None
                if isinstance(edited, dict) and edited_ts is None:
                    raise ValueError("slack_message_invalid")
                if edited_ts is not None:
                    edited_at, edited_ts = _slack_timestamp(edited_ts)
                else:
                    edited_at = None
                version = edited_ts if isinstance(edited_ts, str) else timestamp
                if len(version) > 255:
                    raise ValueError("slack_version_invalid")
                user = message.get("user") if isinstance(message.get("user"), str) else None
                if user is not None and (len(user) > 255 or "\x00" in user):
                    raise ValueError("slack_message_invalid")
                provider_id = f"{channel_id}/{timestamp}"
                if provider_id in seen_messages:
                    continue
                provider_record = {
                    "provider": "slack",
                    "identity": provider_id,
                    "provider_version": version,
                    "timestamp_basis": "provider_modified" if edited_at is not None else "provider_published",
                    "coverage": "returned_snapshot",
                    "content_truncated": False,
                    "provider_modified_at": edited_at.isoformat() if edited_at is not None else None,
                    "source_fields": {
                        "channel_id": channel_id,
                        "message_ts": timestamp,
                        "thread_ts": thread_ts,
                        "user_id": user,
                    },
                }
                record = IngestionRecord(
                    provider_id=provider_id,
                    content=text,
                    observed_at=observed_at,
                    version=version,
                    metadata={
                        "provider_record": provider_record,
                        "channel_id": channel_id,
                        "message_ts": timestamp,
                        "thread_ts": thread_ts if isinstance(thread_ts, str) else None,
                        "user_id": user,
                    },
                    collected_at=collected,
                )
                record_bytes = len(record.model_dump_json().encode("utf-8"))
                if batch_bytes + record_bytes > MAX_BATCH_BYTES:
                    raise rest.CollectionIncomplete
                records.append(record)
                seen_messages.add(provider_id)
                batch_bytes += record_bytes
            if not more:
                response_metadata = payload.get("response_metadata", {})
                if not isinstance(response_metadata, dict):
                    raise ValueError("slack_cursor_invalid")
                if response_metadata.get("next_cursor") not in (None, ""):
                    raise ValueError("slack_cursor_invalid")
                break
            response_metadata = payload.get("response_metadata")
            next_cursor = response_metadata.get("next_cursor") if isinstance(response_metadata, dict) else None
            if (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096
                    or next_cursor in seen_cursors):
                raise ValueError("slack_cursor_invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise rest.CollectionIncomplete
    return ProviderCollectionPage(records=tuple(records), coverage="returned_snapshot")
