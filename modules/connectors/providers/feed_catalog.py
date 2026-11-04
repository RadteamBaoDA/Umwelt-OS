"""Fixed-scope YouTube and arXiv Atom feeds with bounded parsing and arXiv admission."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit
from xml.etree import ElementTree

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.connectors.providers.news import safe_xml_root
from modules.connectors.public import ConnectorConfig, ProviderCollectionPage, ProviderRateLimited
from modules.ingestion.schemas import IngestionRecord
from modules.knowledge.documents.public import ProviderRecordMetadata
from modules.sources.public import ConnectorSource

_MAX_PAGE_BYTES = 10 * 1024 * 1024
_MAX_MAPPED_BYTES = 9 * 1024 * 1024
_ARXIV_ADVISORY_KEY = 1_380_004_657  # signed int32 form of 0x52413331
_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_HF_RATE_LIMIT_ITEM = re.compile(r'^\s*"(api|pages|resolvers)"\s*;\s*r=(\d+)\s*;\s*t=(\d+)\s*$')


class _TextExtractor(HTMLParser):
    """Extract readable text while ignoring markup and active HTML content."""

    def __init__(self) -> None:
        """Initialize only bounded text state for one provider summary."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Suppress script/style data and separate block-like text nodes."""
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1
        elif tag.lower() in {"p", "br", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave ignored HTML subtrees and preserve readable paragraph breaks."""
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag.lower() in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Retain visible text only, never raw markup or executable content."""
        if not self.skip_depth:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    """Convert feed HTML fragments to normalized readable text with no raw markup."""
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def _provider_record(
    *, provider: str, identity: str, version: str | None, timestamp_basis: str,
    modified_at: datetime | None, content_truncated: bool,
    coverage: str = "returned_snapshot",
    source_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    """Validate the provider-owned metadata envelope before storing it on a record."""
    metadata = ProviderRecordMetadata(
        provider=provider,
        identity=identity,
        provider_version=version,
        timestamp_basis=timestamp_basis,
        coverage=coverage,
        content_truncated=content_truncated,
        provider_modified_at=modified_at,
        source_fields=source_fields or {},
    )
    return metadata.model_dump(mode="json", exclude_none=True)


def provider_feed_url(source: ConnectorSource) -> str:
    """Build only the fixed public YouTube or single-category arXiv feed URL."""
    config = ConnectorConfig.model_validate(source.configuration)
    if source.provider == "youtube" and source.type == "rss":
        channel_id = config.youtube_channel_id
        if channel_id is None or config.history_mode != "returned_snapshot":
            raise ValueError("provider_scope_invalid")
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id, safe='')}"
    if source.provider == "arxiv" and source.type == "rss":
        category = config.arxiv_category
        if category is None or config.history_mode != "returned_snapshot":
            raise ValueError("provider_scope_invalid")
        return f"https://rss.arxiv.org/atom/{quote(category, safe='.-')}"
    raise ValueError("provider_scope_invalid")


def _parse_time(value: str | None) -> datetime | None:
    """Parse an actual RFC3339 or RFC822 provider timestamp as an aware UTC instant."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent  # noqa: FURB162  # keeps exact parsing of the 'Z' suffix
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def send_fence_trace(before_request: Callable[[], Awaitable[None]]) -> Callable[[str, dict[str, object]], Awaitable[None]]:
    """Build an httpx trace hook that reruns the native fence right before request headers hit the wire."""
    async def final_send_fence(event_name: str, info: dict[str, object]) -> None:
        """Recheck original authority after connection admission, before headers send."""
        if event_name.endswith("send_request_headers.started"):
            await before_request()
    return final_send_fence


async def _read_feed(url: str, before_request: Callable[[], Awaitable[None]]) -> bytes:
    """Fetch one fixed feed response with TLS, no redirects, and a 10 MiB cap.

    ``before_request`` runs immediately before the send and aborts it by raising. Failures keep their
    identity for ``_classify``: 429 -> ``ProviderRateLimited``; any other non-2xx (redirects included,
    since they are never followed) -> ``ProviderHttpError`` (5xx/408/425 retryable); transport errors
    (``httpx.HTTPError``/``TimeoutError``/``OSError``) propagate and settle as retryable timeouts.
    """
    # Lazy: rest.py imports this module's ``_retry_deadline`` at import time.
    from modules.connectors.providers.rest import ProviderHttpError

    async with asyncio.timeout(30):
        async with httpx.AsyncClient(  # style-only rewrite skipped to avoid touching control flow
            timeout=httpx.Timeout(30), trust_env=False, follow_redirects=False, verify=True
        ) as client:
            await before_request()
            async with client.stream(
                "GET", url, headers={"Accept": "application/atom+xml, application/xml, text/xml"},
                extensions={"trace": send_fence_trace(before_request)},
            ) as response:
                if response.status_code == 429:
                    deadline = _retry_deadline(response.headers, datetime.now(UTC))
                    raise ProviderRateLimited(deadline)
                if not 200 <= response.status_code < 300:
                    raise ProviderHttpError(response.status_code)
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > _MAX_PAGE_BYTES:
                        raise ValueError("provider_response_too_large")
                return bytes(result)


def _structured_api_reset(structured_limit: str) -> int | None:
    """Parse the structured ``RateLimit`` header and return the ``api`` bucket's reset seconds, if any.

    Raises:
        ValueError: ``provider_rate_deadline_invalid`` for a malformed or duplicated bucket.
    """
    api_reset: int | None = None
    seen_buckets: set[str] = set()
    for item in structured_limit.split(","):
        match = _HF_RATE_LIMIT_ITEM.fullmatch(item)
        if match is None:
            raise ValueError("provider_rate_deadline_invalid")
        bucket, _remaining, reset_seconds = match.groups()
        if bucket in seen_buckets:
            raise ValueError("provider_rate_deadline_invalid")
        seen_buckets.add(bucket)
        if bucket == "api":
            try:
                api_reset = int(reset_seconds)
            except (ValueError, OverflowError):
                # Python can reject extremely long decimal fields before timedelta sees them.
                raise ValueError("provider_rate_deadline_invalid") from None
    return api_reset


def _retry_after_deadline(retry_after: str, now: datetime) -> datetime:
    """Convert a ``Retry-After`` value (delta seconds or HTTP date) to a deadline at least one second out.

    Raises:
        ValueError: ``provider_rate_deadline_invalid`` for non-positive, malformed or overflowing values.
    """
    try:
        seconds = int(retry_after)
    except ValueError:
        try:
            parsed = _parse_time(retry_after)
        except (OverflowError, OSError, ValueError):
            # UTC normalization can overflow even when the supplied local date parsed successfully.
            raise ValueError("provider_rate_deadline_invalid") from None
        if parsed is None:
            raise ValueError("provider_rate_deadline_invalid") from None
        try:
            return max(parsed, now + timedelta(seconds=1))
        except (OverflowError, OSError):
            raise ValueError("provider_rate_deadline_invalid") from None
    if seconds <= 0:
        raise ValueError("provider_rate_deadline_invalid")
    try:
        return now + timedelta(seconds=seconds)
    except (OverflowError, OSError):
        raise ValueError("provider_rate_deadline_invalid") from None


def _epoch_reset_deadline(reset: str, now: datetime) -> datetime:
    """Convert an ``X-RateLimit-Reset`` epoch to a deadline that must lie in the future.

    Raises:
        ValueError: ``provider_rate_deadline_invalid`` for a past, malformed or overflowing value.
    """
    try:
        parsed = datetime.fromtimestamp(float(reset), UTC)
        if parsed > now:
            return parsed
        raise ValueError("provider_rate_deadline_invalid")
    except (ValueError, OverflowError, OSError):
        raise ValueError("provider_rate_deadline_invalid") from None


def _standard_reset_deadline(standard_reset: str, now: datetime) -> datetime:
    """Convert a ``RateLimit-Reset`` delta (positive seconds, fractional allowed) to a deadline.

    Raises:
        ValueError: ``provider_rate_deadline_invalid`` for a non-positive, malformed or overflowing value.
    """
    try:
        standard_seconds = float(standard_reset)
        if standard_seconds <= 0:
            raise ValueError
        return now + timedelta(seconds=standard_seconds)
    except (ValueError, OverflowError):
        raise ValueError("provider_rate_deadline_invalid") from None


def _retry_deadline(headers: httpx.Headers, now: datetime) -> datetime:
    """Return the latest representable supplied deadline; map malformed or overflowing evidence to a fixed error."""
    structured_limit = headers.get("ratelimit")
    api_reset = _structured_api_reset(structured_limit) if structured_limit is not None else None
    deadlines: list[datetime] = []
    if api_reset is not None:
        try:
            # Hugging Face documents `t` as seconds remaining until the bucket reset.
            deadlines.append(now + timedelta(seconds=api_reset))
        except OverflowError:
            raise ValueError("provider_rate_deadline_invalid") from None
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        deadlines.append(_retry_after_deadline(retry_after, now))
    reset = headers.get("x-ratelimit-reset")
    if reset is not None:
        deadlines.append(_epoch_reset_deadline(reset, now))
    standard_reset = headers.get("ratelimit-reset")
    if standard_reset is not None:
        deadlines.append(_standard_reset_deadline(standard_reset, now))
    if deadlines:
        # Multiple independent headers can impose different waits; never resume at the shorter one.
        return max(deadlines)
    # ponytail: absent retry/reset evidence uses a one-minute finite fallback; malformed supplied deadlines fail closed.
    return now + timedelta(minutes=1)


def _selected_hash(values: Mapping[str, object]) -> str:
    """Hash selected provider metadata deterministically without treating hash as time."""
    raw = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _feed_canonical(entry: ElementTree.Element, provider: str, identity: str) -> str | None:
    """Retain only actual alternate links that match the selected provider identity."""
    link = entry.find(f"{{{_ATOM}}}link[@rel='alternate']")
    href = link.attrib.get("href") if link is not None else None
    if not href:
        return None
    parsed = urlsplit(href)
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.fragment:
        return None
    if provider == "youtube":
        if parsed.scheme != "https":
            return None
        query = parse_qs(parsed.query)
        if (
            parsed.netloc == "www.youtube.com" and parsed.path == "/watch"
            and set(query) == {"v"} and query["v"] == [identity.rsplit(":", 1)[-1]]
        ):
            return href
    elif provider == "arxiv":
        if parsed.netloc == "arxiv.org" and parsed.path.startswith("/abs/"):
            return href
    return None


def _same_youtube_channel(feed_id: str | None, configured: str | None) -> bool:
    """Return True when two YouTube channel ids name the same channel.

    The Atom feed's ``yt:channelId`` is sometimes the full ``UC``+22-char id and
    sometimes the bare 22-char form. Only a leading ``UC`` on a 24-char id is
    stripped, so distinct channels never compare equal.
    """
    def bare(value: str | None) -> str | None:
        """Strip the ``UC`` prefix from a 24-char channel id; leave other values untouched."""
        return value[2:] if value and len(value) == 24 and value.startswith("UC") else value

    return feed_id is not None and bare(feed_id) == bare(configured)


@dataclass(frozen=True)
class _FeedEntry:
    """Provider-neutral mapped Atom entry before hashing and record assembly."""

    provider_id: str
    title: str
    author: str
    tags: list[str]
    text_value: str
    selected: dict[str, object]
    source_fields: dict[str, object]
    source_fields_truncated: bool
    basis: str
    observed: datetime
    modified: datetime | None
    published_raw: str | None
    modified_raw: str | None
    canonical_url: str | None


# An entry mapper returns ``None`` when the entry is outside the configured scope (the page is then truncated).
_EntryMapper = Callable[[ElementTree.Element, str | None, str, str, datetime], "_FeedEntry | None"]


def _timestamp_basis(modified: datetime | None, published: datetime | None) -> str:
    """Prefer provider-modified, then provider-published, else the collection clock."""
    return "provider_modified" if modified else "provider_published" if published else "collection"


def _youtube_entry(
    entry: ElementTree.Element, scope: str | None, title: str, summary: str, collected_at: datetime,
) -> _FeedEntry | None:
    """Map one YouTube Atom entry, or ``None`` when its channel/video identity is out of scope."""
    channel_id = entry.findtext(f"{{{_YT}}}channelId")
    video_id = entry.findtext(f"{{{_YT}}}videoId")
    if not _same_youtube_channel(channel_id, scope) or not video_id or len(video_id) > 128:
        return None
    published_raw = entry.findtext(f"{{{_ATOM}}}published")
    modified_raw = entry.findtext(f"{{{_ATOM}}}updated")
    published = _parse_time(published_raw)
    modified = _parse_time(modified_raw)
    identity = f"youtube:{scope}:{video_id}"
    author = _plain_text(entry.findtext(f"{{{_ATOM}}}author/{{{_ATOM}}}name") or "")
    category_nodes = entry.findall(f"{{{_ATOM}}}category")
    tags = [node.attrib["term"] for node in category_nodes if node.attrib.get("term")]
    selected: dict[str, object] = {
        "title": title, "summary": summary, "author": author,
        "published": published_raw, "updated": modified_raw, "tags": tags,
    }
    source_fields: dict[str, object] = {
        "title": title[:500],
        "summary": summary[:4000],
        "tags": [value[:255] for value in tags[:100]],
    }
    if author:
        source_fields["author"] = author[:1000]
    if published_raw is not None:
        source_fields["published_at"] = published_raw[:64]
    if modified_raw is not None:
        source_fields["provider_updated_at"] = modified_raw[:64]
    source_fields_truncated = (
        len(title) > 500 or len(summary) > 4000 or len(author) > 1000
        or len(tags) > 100 or any(len(value) > 255 for value in tags)
        or (published_raw is not None and len(published_raw) > 64)
        or (modified_raw is not None and len(modified_raw) > 64)
    )
    return _FeedEntry(
        provider_id=identity, title=title, author=author, tags=tags,
        text_value=title + (f"\n\n{summary}" if summary else ""), selected=selected,
        source_fields=source_fields, source_fields_truncated=source_fields_truncated,
        basis=_timestamp_basis(modified, published), observed=modified or published or collected_at,
        modified=modified, published_raw=published_raw, modified_raw=modified_raw,
        canonical_url=_feed_canonical(entry, "youtube", identity),
    )


def _arxiv_entry(
    entry: ElementTree.Element, scope: str | None, title: str, summary: str, collected_at: datetime,
) -> _FeedEntry | None:
    """Map one arXiv Atom entry, or ``None`` when it has no usable identifier."""
    arxiv_id = entry.findtext(f"{{{_ATOM}}}id")
    if not arxiv_id or len(arxiv_id) > 512:
        return None
    published_raw = entry.findtext(f"{{{_ATOM}}}published")
    modified_raw = entry.findtext(f"{{{_ATOM}}}updated")
    published = _parse_time(published_raw)
    modified = _parse_time(modified_raw)
    author_nodes = entry.findall(f"{{{_ATOM}}}author")
    author_names = [
        _plain_text(node.findtext(f"{{{_ATOM}}}name") or "")
        for node in author_nodes
    ]
    category_nodes = entry.findall("{http://www.w3.org/2005/Atom}category")
    categories = [
        node.attrib.get("term", "")
        for node in category_nodes
        if node.attrib.get("term")
    ]
    author = ", ".join(author_names)
    source_fields: dict[str, object] = {
        "title": title[:500],
        "summary": summary[:4000],
        "authors": [value[:255] for value in author_names[:100]],
        "categories": [value[:255] for value in categories[:100]],
        "tags": [value[:255] for value in categories[:100]],
    }
    if author:
        source_fields["author"] = author[:1000]
    if published_raw is not None:
        source_fields["published_at"] = published_raw[:64]
    if modified_raw is not None:
        source_fields["provider_updated_at"] = modified_raw[:64]
    source_fields_truncated = (
        len(title) > 500 or len(summary) > 4000 or len(author) > 1000
        or len(author_nodes) > 100 or len(category_nodes) > 100
        or any(len(value) > 255 for value in [*author_names, *categories])
        or (published_raw is not None and len(published_raw) > 64)
        or (modified_raw is not None and len(modified_raw) > 64)
    )
    selected: dict[str, object] = {
        "title": title, "summary": summary, "authors": author_names, "categories": categories,
        "published": published_raw, "updated": modified_raw,
    }
    return _FeedEntry(
        provider_id=arxiv_id, title=title, author=author, tags=categories,
        text_value=title + (f"\n\n{summary}" if summary else ""), selected=selected,
        source_fields=source_fields, source_fields_truncated=source_fields_truncated,
        basis=_timestamp_basis(modified, published), observed=modified or published or collected_at,
        modified=modified, published_raw=published_raw, modified_raw=modified_raw,
        canonical_url=_feed_canonical(entry, "arxiv", arxiv_id),
    )


# Provider -> entry mapper registry: a new Atom-feed provider registers here without touching the loop.
_ENTRY_MAPPERS: dict[str, _EntryMapper] = {"youtube": _youtube_entry, "arxiv": _arxiv_entry}


def _feed_record(provider: str, mapped: _FeedEntry, truncated: bool, collected_at: datetime) -> IngestionRecord:
    """Assemble the ingestion record (version hash, provenance, bounded metadata) for a mapped entry.

    ``truncated`` is the page truncation state at this point of the sweep (it feeds coverage).
    """
    digest = _selected_hash(mapped.selected)
    raw_version = f"{mapped.modified.isoformat()}:{digest}" if mapped.modified is not None else digest
    version = raw_version[:255]
    content_truncated = len(mapped.text_value) > 4000 or mapped.source_fields_truncated
    record_metadata: dict[str, object] = {
        "provider_record": _provider_record(
            provider=provider,
            identity=mapped.provider_id,
            version=version,
            timestamp_basis=mapped.basis,
            modified_at=mapped.modified,
            content_truncated=content_truncated,
            coverage="truncated" if truncated else "returned_snapshot",
            source_fields=mapped.source_fields,
        ),
        "author": mapped.author[:1000],
        "tags": mapped.tags[:100],
        "title": mapped.title[:500],
    }
    if mapped.canonical_url is not None:
        record_metadata["canonical_url"] = mapped.canonical_url
    if mapped.published_raw is not None:
        record_metadata["published_at"] = mapped.published_raw[:64]
    if mapped.modified_raw is not None:
        record_metadata["provider_updated_at"] = mapped.modified_raw[:64]
    return IngestionRecord(
        provider_id=mapped.provider_id,
        content=mapped.text_value[:4000],
        observed_at=mapped.observed,
        version=version,
        metadata=record_metadata,
        collected_at=collected_at,
    )


def _mark_truncated(records: list[IngestionRecord]) -> None:
    """Rewrite each record's provenance coverage to ``truncated`` in place."""
    for index, record in enumerate(records):
        metadata = dict(record.metadata)
        provider_record = ProviderRecordMetadata.model_validate(
            metadata["provider_record"]
        ).model_copy(update={"coverage": "truncated"})
        metadata["provider_record"] = provider_record.model_dump(
            mode="json", exclude_none=True
        )
        records[index] = record.model_copy(update={"metadata": metadata})


def _records_from_feed(
    source: ConnectorSource, body: bytes, collected_at: datetime
) -> ProviderCollectionPage:
    """Map actual scoped YouTube/arXiv Atom entries, limiting output and preserving source clocks.

    Out-of-scope entries and the 500-entry / mapped-byte caps mark the page truncated; the
    coverage already stamped on earlier records is rewritten at the end.
    """
    config = ConnectorConfig.model_validate(source.configuration)
    # DOCTYPE/ENTITY refused (billion-laughs / XXE class); size bound matches the page fetch cap
    # (live arXiv category Atom feeds exceed the 1 MiB default feed limit).
    root = safe_xml_root(body, max_bytes=_MAX_PAGE_BYTES)
    records: list[IngestionRecord] = []
    mapped_bytes = 0
    entries = root.findall(f"{{{_ATOM}}}entry")
    truncated = len(entries) > 500
    scope = config.youtube_channel_id if source.provider == "youtube" else config.arxiv_category
    for entry in entries[:500]:
        title = _plain_text(entry.findtext(f"{{{_ATOM}}}title") or "")
        summary = _plain_text(entry.findtext(f"{{{_ATOM}}}summary") or entry.findtext(f"{{{_ATOM}}}content") or "")
        provider = source.provider
        mapper = _ENTRY_MAPPERS.get(provider) if provider is not None else None
        if provider is None or mapper is None:
            raise ValueError("provider_scope_invalid")
        mapped = mapper(entry, scope, title, summary, collected_at)
        if mapped is None:
            truncated = True
            continue
        record = _feed_record(provider, mapped, truncated, collected_at)
        record_bytes = len(record.model_dump_json().encode("utf-8"))
        if mapped_bytes + record_bytes > _MAX_MAPPED_BYTES:
            truncated = True
            break
        mapped_bytes += record_bytes
        records.append(record)
    if truncated:
        _mark_truncated(records)
    return ProviderCollectionPage(
        records=tuple(records),
        coverage="truncated" if truncated else "returned_snapshot",
        next_eligible_at=None,
    )


async def collect_provider_feed(
    source: ConnectorSource,
    *,
    collected_at: datetime,
    session_factory: async_sessionmaker[AsyncSession],
    before_request: Callable[[], Awaitable[None]],
) -> ProviderCollectionPage:
    """Collect one bounded public Atom snapshot and serialize arXiv requests across workers.

    ``before_request`` is the native every-physical-send fence callback.
    """
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must be timezone-aware")
    url = provider_feed_url(source)
    if source.provider == "youtube":
        try:
            async with asyncio.timeout(30):
                body = await _read_feed(url, before_request)
        except ProviderRateLimited as exc:
            return ProviderCollectionPage(records=(), coverage="returned_snapshot", next_eligible_at=exc.next_eligible_at)
        return await asyncio.to_thread(_records_from_feed, source, body, collected_at)
    if source.provider != "arxiv":
        raise ValueError("provider_scope_invalid")
    async with asyncio.timeout(60):
        async with session_factory() as session:  # style-only rewrite skipped to avoid touching control flow  # noqa: SIM117  # merging would keep the DB session open across the CPU-bound parse that follows
            async with session.begin():
                locked = await session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:provider_key)"),
                    {"provider_key": _ARXIV_ADVISORY_KEY},
                )
                if not locked:
                    return ProviderCollectionPage(
                        records=(), coverage="returned_snapshot",
                        next_eligible_at=datetime.now(UTC) + timedelta(seconds=3),
                    )
                spacing_started = time.monotonic()
                await asyncio.sleep(max(0.0, 3.0 - (time.monotonic() - spacing_started)))
                # ponytail: one global key serializes every arXiv source; per-category locks only if upstream permits higher throughput.
                try:
                    body = await _read_feed(url, before_request)
                except ProviderRateLimited as exc:
                    return ProviderCollectionPage(records=(), coverage="returned_snapshot", next_eligible_at=exc.next_eligible_at)
        return await asyncio.to_thread(_records_from_feed, source, body, collected_at)
