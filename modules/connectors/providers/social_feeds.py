"""Bounded Bluesky public profile-feed mapping for the shared native executor."""

# ValueError is the executor's stable classification for invalid provider payloads.
# ruff: noqa: TRY004

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, urlencode

from modules.connectors.providers.rest import Fetched
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.schemas import ProviderRecordMetadata

_MAX_RECORDS = 100
_MAX_TEXT = 20_000
_MAX_CURSOR_BYTES = 2048


@dataclass(frozen=True)
class BlueskyCollectionPage:
    """One bounded public feed page and its scope-bound receipt cursor."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str | None
    coverage: Literal["returned_snapshot", "truncated"]


def _utc(value: object) -> datetime | None:
    """Parse only aware provider timestamps and normalize them to UTC."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _text_record(
    identity: str,
    content: str,
    observed_at: datetime,
    collected_at: datetime,
    *,
    canonical_url: str | None,
    author: str | None,
    version: str | None = None,
) -> IngestionRecord:
    """Build a Bluesky record with bounded text and validated typed provenance."""
    truncated = len(content) > _MAX_TEXT
    metadata: dict[str, object] = {
        "provider": "bluesky",
        "source_id": identity[:512],
        "author": author[:255] if author else None,
        "canonical_url": canonical_url[:2048] if canonical_url and len(canonical_url) <= 2048 else None,
        "content_truncated": truncated,
    }
    source_fields: dict[str, object] = {"created_at": observed_at.isoformat()}
    if author:
        source_fields["author"] = author[:255]
    if canonical_url:
        source_fields["canonical_url"] = canonical_url
    metadata["provider_record"] = ProviderRecordMetadata(
        provider="bluesky", identity=identity[:512], provider_version=version[:255] if version else None,
        timestamp_basis="provider_published", coverage="returned_snapshot", content_truncated=truncated,
        source_fields=source_fields,
    ).model_dump(mode="json", exclude_none=True)
    return IngestionRecord(
        provider_id=identity[:512], content=content[:_MAX_TEXT], observed_at=observed_at,
        version=version[:255] if version else None, metadata=metadata, collected_at=collected_at,
    )


def _page(
    rows: list[IngestionRecord], *, cursor_after: str | None,
) -> BlueskyCollectionPage:
    """Return one bounded page and align per-record provenance with page coverage."""
    bounded = rows[:_MAX_RECORDS]
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if cursor_after else "returned_snapshot"
    if coverage == "truncated":
        for index, record in enumerate(bounded):
            raw = record.metadata.get("provider_record")
            if raw is None:
                continue
            provider_record = ProviderRecordMetadata.model_validate(raw).model_copy(update={"coverage": "truncated"})
            metadata = {**record.metadata, "provider_record": provider_record.model_dump(mode="json", exclude_none=True)}
            bounded[index] = record.model_copy(update={"metadata": metadata})
    return BlueskyCollectionPage(tuple(bounded), cursor_after, coverage)


def _read_cursor(raw: str | None, actor: str) -> str | None:
    """Validate an opaque AppView cursor against the exact profile whose feed created it."""
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise ValueError("bluesky_cursor_invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("bluesky_cursor_invalid") from exc
    page = value.get("page") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("v") != 1 or value.get("actor") != actor
            or not isinstance(page, str) or not page or len(page) > _MAX_CURSOR_BYTES):
        raise ValueError("bluesky_cursor_scope_changed" if isinstance(value, dict) and value.get("actor") != actor
                         else "bluesky_cursor_invalid")
    return page


def _write_cursor(actor: str, page: object) -> str | None:
    """Encode a bounded AppView cursor with its actor scope for atomic receipt storage."""
    if page is None:
        return None
    if not isinstance(page, str) or not page or len(page) > _MAX_CURSOR_BYTES:
        raise ValueError("bluesky_cursor_invalid")
    encoded = json.dumps({"v": 1, "actor": actor, "page": page}, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise ValueError("bluesky_cursor_invalid")
    return encoded


def map_bluesky_profile_payload(
    payload: object,
    actor: str,
    *,
    collected_at: datetime,
) -> BlueskyCollectionPage:
    """Map one public author-feed response, preserving its bounded pagination cursor."""
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("feed"), list):
        raise ValueError("bluesky_response_invalid")
    if len(payload["feed"]) > _MAX_RECORDS:
        raise ValueError("bluesky_page_too_large")
    rows: list[IngestionRecord] = []
    for item in payload["feed"][:_MAX_RECORDS]:
        post = item.get("post") if isinstance(item, Mapping) else None
        record = post.get("record") if isinstance(post, Mapping) else None
        uri = post.get("uri") if isinstance(post, Mapping) else None
        cid = post.get("cid") if isinstance(post, Mapping) else None
        author_obj = post.get("author") if isinstance(post, Mapping) else None
        author = author_obj.get("handle") if isinstance(author_obj, Mapping) else actor
        text = record.get("text") if isinstance(record, Mapping) else None
        created = _utc(record.get("createdAt")) if isinstance(record, Mapping) else None
        if not isinstance(uri, str) or not isinstance(cid, str) or not isinstance(text, str) or created is None:
            raise ValueError("bluesky_response_invalid")
        if len(uri) > 512 or len(cid) > 255:
            raise ValueError("bluesky_response_invalid")
        post_url = (
            f"https://bsky.app/profile/{quote(author, safe='')}/post/{quote(uri.rsplit('/', 1)[-1], safe='')}"
            if isinstance(author, str) else None
        )
        rows.append(_text_record(uri, text, created, collected_at, canonical_url=post_url,
                                 author=author if isinstance(author, str) else None, version=cid))
    return _page(rows, cursor_after=_write_cursor(actor, payload.get("cursor")))


async def collect_bluesky_profile(
    actor: str,
    *,
    collected_at: datetime,
    fetch_bytes: Callable[[str], Awaitable[Fetched]],
    cursor: str | None = None,
) -> BlueskyCollectionPage:
    """Fetch one bounded public profile page and return its receipt-committed continuation."""
    actor = actor.strip()
    if collected_at.tzinfo is None or collected_at.utcoffset() is None or not 1 <= len(actor) <= 253:
        raise ValueError("provider_scope_invalid")
    params = {"actor": actor, "limit": "100", "filter": "posts_no_replies"}
    page_cursor = _read_cursor(cursor, actor)
    if page_cursor:
        params["cursor"] = page_cursor
    url = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?" + urlencode(params)
    response = await fetch_bytes(url)
    if response.body is None:
        raise ValueError("bluesky_response_invalid")
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("bluesky_response_invalid") from None
    return map_bluesky_profile_payload(payload, actor, collected_at=collected_at)
