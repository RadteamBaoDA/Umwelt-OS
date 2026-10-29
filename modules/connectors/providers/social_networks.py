"""Read-only Reddit, Mastodon and X collectors for the shared native executor.

Each adapter uses one bounded response per trigger except account resolution on Mastodon/X,
which is separately bounded. The caller supplies the scheduler's DNS-pinned transport and
send-fence; returned cursors must be committed with the same durable ingestion receipt.
"""

# ValueError is the executor's stable classification for invalid provider payloads and scopes.
# ruff: noqa: TRY004

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import quote, urlencode

from modules.connectors.providers import rest
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.schemas import ProviderRecordMetadata

MAX_RECORDS = 100
MAX_TEXT = 20_000
MAX_CURSOR_BYTES = 1024
_X_ORIGIN = "https://api.x.com"
_REDDIT_ORIGIN = "https://oauth.reddit.com"


@dataclass(frozen=True)
class SocialCollectionResult:
    """Records and continuation state that the caller commits atomically with its receipt."""

    records: tuple[IngestionRecord, ...]
    cursor_after: str | None
    coverage: Literal["returned_snapshot", "truncated"]


class _HtmlText(HTMLParser):
    """Collect visible text nodes from provider HTML while ignoring markup and comments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Suppress non-visible executable/style text while scanning HTML fragments."""
        if tag.lower() in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Restore visible-text collection after a suppressed element closes."""
        if tag.lower() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        """Retain only text nodes, never provider-supplied HTML markup."""
        if not self.hidden_depth:
            self.parts.append(data)


def _plain_text(value: str, limit: int = MAX_TEXT) -> str:
    """Convert untrusted provider HTML to bounded plain text without retaining markup."""
    parser = _HtmlText()
    parser.feed(value[: limit * 4])
    return " ".join(" ".join(parser.parts).split())[:limit]


def _cursor(provider: str, value: str | None) -> str | None:
    """Encode one bounded provider continuation token for receipt-atomic persistence."""
    if value is None:
        return None
    if not value or len(value.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise rest.CollectionIncomplete
    encoded = json.dumps({"v": 1, "provider": provider, "page": value}, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise rest.CollectionIncomplete
    return encoded


def _read_cursor(raw: str | None, provider: str) -> str | None:
    """Validate a persisted provider cursor before sending it back to its original API."""
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise ValueError("social_cursor_invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("social_cursor_invalid") from exc
    page = value.get("page") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("v") != 1 or value.get("provider") != provider
            or not isinstance(page, str) or not page):
        raise ValueError("social_cursor_invalid")
    return page


def _record(
    *, provider: str, identity: str, version: str | None, content: str,
    observed_at: datetime, collected_at: datetime, source_fields: dict[str, object],
    timestamp_basis: Literal["provider_modified", "provider_published", "collection"] = "provider_published",
) -> IngestionRecord:
    """Create a bounded social post with typed provenance matching its durable identity/version."""
    if not identity or len(identity) > 512 or observed_at.tzinfo is None or collected_at.tzinfo is None:
        raise ValueError("social_record_invalid")
    envelope = ProviderRecordMetadata(
        provider=provider, identity=identity, provider_version=version,
        timestamp_basis=timestamp_basis, coverage="returned_snapshot",
        content_truncated=len(content) > MAX_TEXT, source_fields=source_fields,
    )
    return IngestionRecord(
        provider_id=identity, version=version[:255] if version else None,
        content=content[:MAX_TEXT], observed_at=observed_at.astimezone(UTC),
        collected_at=collected_at.astimezone(UTC),
        metadata={"provider_record": envelope.model_dump(mode="json", exclude_none=True)},
    )


def _finish(
    provider: str, records: list[IngestionRecord], page: str | None,
) -> SocialCollectionResult:
    """Align record and page coverage before exposing a continuation cursor."""
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if page else "returned_snapshot"
    if coverage == "truncated":
        for index, record in enumerate(records):
            raw = record.metadata["provider_record"]
            envelope = ProviderRecordMetadata.model_validate(raw).model_copy(update={"coverage": coverage})
            records[index] = record.model_copy(update={
                "metadata": {**record.metadata, "provider_record": envelope.model_dump(mode="json", exclude_none=True)},
            })
    return SocialCollectionResult(tuple(records), _cursor(provider, page), coverage)


async def collect_reddit_subreddit(
    subreddit: str, access_token: str, *, cursor: str | None, collected_at: datetime,
    fetch_bytes: Callable[..., Awaitable[rest.Fetched]],
) -> SocialCollectionResult:
    """Collect one newest-page snapshot from an OAuth-authorized public subreddit.

    Only the read endpoint is used. The API continuation token is returned with matching
    ``truncated`` coverage; access token refresh and all source fences belong to the caller.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit) or not access_token:
        raise ValueError("reddit_scope_invalid")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collection_time_invalid")
    after = _read_cursor(cursor, "reddit")
    params: dict[str, str] = {"limit": str(MAX_RECORDS), "raw_json": "1"}
    if after:
        params["after"] = after
    response = await fetch_bytes(
        f"{_REDDIT_ORIGIN}/r/{quote(subreddit, safe='')}/new.json?{urlencode(params)}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        max_bytes=rest.MAX_PAGE_BYTES,
    )
    payload = _json(response, "reddit")
    if not isinstance(payload, dict):
        raise ValueError("reddit_response_invalid")
    listing = payload.get("data")
    if not isinstance(listing, dict):
        raise ValueError("reddit_response_invalid")
    children = listing.get("children")
    if not isinstance(children, list):
        raise ValueError("reddit_response_invalid")
    records: list[IngestionRecord] = []
    for item in children[:MAX_RECORDS]:
        post = item.get("data") if isinstance(item, dict) else None
        if not isinstance(post, dict):
            raise ValueError("reddit_response_invalid")
        identifier, permalink, title = post.get("id"), post.get("permalink"), post.get("title")
        if (
            not isinstance(identifier, str) or not identifier
            or not isinstance(permalink, str) or not permalink
            or not isinstance(title, str) or not title
        ):
            raise ValueError("reddit_response_invalid")
        created = _timestamp(post.get("created_utc"))
        raw_author = post.get("author")
        author: str = raw_author if isinstance(raw_author, str) else ""
        body = post.get("selftext") if isinstance(post.get("selftext"), str) else ""
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else None
        if url is None:
            raise ValueError("reddit_link_invalid")
        content = str(title) + (f"\n\n{body}" if body else "")
        records.append(_record(
            provider="reddit", identity=f"t3_{identifier}", version=f"t3_{identifier}",
            content=content, observed_at=created, collected_at=collected_at,
            source_fields={
                "title": str(title)[:500], "subreddit": subreddit,
                "author": author[:255], "canonical_url": url,
                "created_at": created.isoformat(),
            },
        ))
    next_after = listing.get("after")
    if next_after is not None and (not isinstance(next_after, str) or len(next_after) > MAX_CURSOR_BYTES):
        raise ValueError("reddit_cursor_invalid")
    return _finish("reddit", records, next_after,)


def _json(response: rest.Fetched, provider: str) -> object:
    """Decode a bounded social response without surfacing provider content or credentials."""
    if response.body is None:
        raise ValueError(f"{provider}_response_invalid")
    try:
        value = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{provider}_response_invalid") from exc
    return value


def _timestamp(value: object) -> datetime:
    """Normalize a numeric epoch or aware provider timestamp and reject fabricated times."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(UTC)
    except (ValueError, OSError, OverflowError):
        pass
    raise ValueError("social_timestamp_invalid")


async def collect_mastodon_scope(
    instance_url: str, *, account: str | None, hashtag: str | None, access_token: str | None,
    cursor: str | None, collected_at: datetime,
    fetch_bytes: Callable[..., Awaitable[rest.Fetched]],
) -> SocialCollectionResult:
    """Collect one public Mastodon account or hashtag page from its explicitly selected instance.

    Instance origin is validated by the caller and revalidated by the DNS-pinned transport.
    Optional tokens are bearer-only; only public visibility posts enter the source snapshot.
    """
    origin = _validated_instance(instance_url)
    if bool(account) == bool(hashtag):
        raise ValueError("mastodon_scope_invalid")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collection_time_invalid")
    page = _read_cursor(cursor, "mastodon")
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if hashtag:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", hashtag):
            raise ValueError("mastodon_hashtag_invalid")
        params = {"limit": "40"}
        if page:
            params["max_id"] = page
        response = await fetch_bytes(
            f"{origin}/api/v1/timelines/tag/{quote(hashtag, safe='')}?{urlencode(params)}",
            headers=headers, max_bytes=rest.MAX_PAGE_BYTES,
        )
        account_id = None
    else:
        if not isinstance(account, str) or not re.fullmatch(r"@?[A-Za-z0-9_.-]{1,64}(?:@[A-Za-z0-9.-]{1,253})?", account):
            raise ValueError("mastodon_account_invalid")
        if page and page.startswith("account:"):
            account_id, page = page[8:].split("|", 1)
        else:
            lookup = await fetch_bytes(
                f"{origin}/api/v1/accounts/lookup?{urlencode({'acct': account.lstrip('@')})}",
                headers=headers, max_bytes=256 * 1024,
            )
            account_payload = _json(lookup, "mastodon")
            account_id = account_payload.get("id") if isinstance(account_payload, dict) else None
            if not isinstance(account_id, str) or not account_id.isdigit():
                raise ValueError("mastodon_account_invalid")
        params = {"limit": "40"}
        if page:
            params["max_id"] = page
        response = await fetch_bytes(
            f"{origin}/api/v1/accounts/{quote(account_id, safe='')}/statuses?{urlencode(params)}",
            headers=headers, max_bytes=rest.MAX_PAGE_BYTES,
        )
    payload = _json(response, "mastodon")
    if not isinstance(payload, list):
        raise ValueError("mastodon_response_invalid")
    records: list[IngestionRecord] = []
    for status in payload[:40]:
        if not isinstance(status, dict) or status.get("visibility") != "public":
            continue
        identifier, url, created = status.get("id"), status.get("url"), _timestamp(status.get("created_at"))
        content_html = status.get("content")
        if not isinstance(identifier, str) or not identifier.isdigit() or not isinstance(content_html, str):
            raise ValueError("mastodon_response_invalid")
        parsed_url = _validated_public_link(url)
        raw_account = status.get("account")
        account_obj: dict[str, Any] = raw_account if isinstance(raw_account, dict) else {}
        raw_acct = account_obj.get("acct")
        acct: str = raw_acct if isinstance(raw_acct, str) else ""
        text = _plain_text(content_html)
        records.append(_record(
            provider="mastodon", identity=identifier,
            version=status.get("edited_at") if isinstance(status.get("edited_at"), str) else identifier,
            content=text, observed_at=created, collected_at=collected_at,
            source_fields={"author": acct[:255], "canonical_url": parsed_url,
                           "created_at": created.isoformat()},
        ))
    # Never trust provider pagination URLs; continue only with the validated status ID.
    next_id = payload[-1].get("id") if payload and isinstance(payload[-1], dict) else None
    next_cursor = f"account:{account_id}|{next_id}" if account and isinstance(next_id, str) else next_id
    return _finish("mastodon", records, next_cursor if len(payload) >= 40 else None)


async def collect_x_scope(
    *, account: str | None, query: str | None, access_token: str, cursor: str | None,
    collected_at: datetime, fetch_bytes: Callable[..., Awaitable[rest.Fetched]],
) -> SocialCollectionResult:
    """Collect one read-only X account-timeline or recent-search page using app bearer access."""
    if bool(account) == bool(query) or not access_token:
        raise ValueError("x_scope_invalid")
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collection_time_invalid")
    token = _read_cursor(cursor, "x")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    common = {"max_results": "100", "tweet.fields": "author_id,created_at,lang,edit_history_tweet_ids"}
    account_id: str | None = None
    if account:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account):
            raise ValueError("x_account_invalid")
        if token and token.startswith("user:"):
            account_id, token = token[5:].split("|", 1)
        else:
            lookup = await fetch_bytes(
                f"{_X_ORIGIN}/2/users/by/username/{quote(account, safe='')}",
                headers=headers, max_bytes=256 * 1024,
            )
            user_payload = _json(lookup, "x")
            user = user_payload.get("data") if isinstance(user_payload, dict) else None
            account_id = user.get("id") if isinstance(user, dict) else None
            if not isinstance(account_id, str) or not account_id.isdigit():
                raise ValueError("x_account_invalid")
        params = {**common, "exclude": "retweets,replies"}
        if token:
            params["pagination_token"] = token
        response = await fetch_bytes(
            f"{_X_ORIGIN}/2/users/{quote(account_id, safe='')}/tweets?{urlencode(params)}",
            headers=headers, max_bytes=rest.MAX_PAGE_BYTES,
        )
    else:
        cleaned_query = " ".join((query or "").split())
        if not 1 <= len(cleaned_query) <= 512 or re.search(r"[\x00-\x1f\x7f]", cleaned_query):
            raise ValueError("x_query_invalid")
        params = {**common, "query": cleaned_query}
        if token:
            params["next_token"] = token
        response = await fetch_bytes(
            f"{_X_ORIGIN}/2/tweets/search/recent?{urlencode(params)}",
            headers=headers, max_bytes=rest.MAX_PAGE_BYTES,
        )
    payload = _json(response, "x")
    if not isinstance(payload, dict):
        raise ValueError("x_response_invalid")
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise ValueError("x_response_invalid")
    records: list[IngestionRecord] = []
    for tweet in data[:MAX_RECORDS]:
        if not isinstance(tweet, dict) or not isinstance(tweet.get("id"), str) or not isinstance(tweet.get("text"), str):
            raise ValueError("x_response_invalid")
        identifier = tweet["id"]
        created = _timestamp(tweet.get("created_at"))
        raw_author_id = tweet.get("author_id")
        author_id = raw_author_id if isinstance(raw_author_id, str) else account_id or ""
        url = f"https://x.com/i/web/status/{quote(identifier, safe='')}"
        source_fields: dict[str, object] = {
            "author_id": author_id[:255], "canonical_url": url, "created_at": created.isoformat(),
        }
        if isinstance(tweet.get("lang"), str):
            source_fields["language"] = tweet["lang"][:64]
        records.append(_record(
            provider="x", identity=identifier, version=identifier, content=tweet["text"],
            observed_at=created, collected_at=collected_at, source_fields=source_fields,
        ))
    meta = payload.get("meta", {})
    next_token = meta.get("next_token") if isinstance(meta, dict) else None
    if next_token is not None and (not isinstance(next_token, str) or len(next_token) > MAX_CURSOR_BYTES):
        raise ValueError("x_cursor_invalid")
    next_cursor = f"user:{account_id}|{next_token}" if account and isinstance(next_token, str) else next_token
    return _finish("x", records, next_cursor)


def _validated_instance(value: str) -> str:
    """Accept only an HTTPS Mastodon instance origin without paths, credentials or nonstandard ports."""
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("mastodon_instance_invalid")
    return f"https://{parsed.hostname.lower()}"


def _validated_public_link(value: object) -> str:
    """Require original post links to use HTTPS and contain no embedded credentials."""
    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("social_link_invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("social_link_invalid")
    return value


__all__ = ["SocialCollectionResult", "collect_mastodon_scope", "collect_reddit_subreddit", "collect_x_scope"]
