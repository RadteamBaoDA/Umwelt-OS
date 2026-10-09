"""Pure news adapters: fixed RSS/Atom presets (BBC World, VnExpress Business) and the Hacker News seam.

No network and no database. Request builders return fixed https requests for an injected transport;
mappers turn already-fetched bodies into typed ``IngestionRecord`` envelopes with NO ``world_data``.

Conventions
- Hostile XML is refused, not sanitized: strict UTF-8, declared encoding must be utf-8, any DOCTYPE or
  ENTITY is rejected before parsing (defusedxml is not installed; stdlib ElementTree never sees one).
- Identity is the feed GUID (Atom id), else the https link; never the position in the feed. Version is a
  hash of selected content only, so reordering, repeated fetches and clock changes do not change it.
- ``observed_at`` is the parsed publication (else update) instant; with no usable date it is
  ``collected_at`` and ``timestamp_basis="collection"`` (the version still excludes it).
- Records carry the feed title/summary only (``content_scope="feed_summary"``); the linked article is a
  citation and is never fetched, and nothing here claims its full text is available.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    content_version,
    decode_json,
)
from modules.ingestion.schemas import IngestionRecord

MAX_FEED_BYTES = 1024 * 1024
MAX_FEED_ITEMS_SCANNED = 500
MAX_FEED_ITEMS = 100
_MAX_SUMMARY = 4000
_MAX_TITLE = 500
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

FEED_URLS: dict[str, str] = {
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "vnexpress_business": "https://vnexpress.net/rss/kinh-doanh.rss",
}
PUBLISHERS: dict[str, str] = {"bbc_world": "BBC News", "vnexpress_business": "VnExpress"}

# Google News RSS search: unofficial endpoint, no API contract. The URL is built here from validated scope only.
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_LICENSE = "Google News RSS / publisher"
GOOGLE_NEWS_SITES = ("any", "reuters.com", "apnews.com", "bbc.com", "vnexpress.net")
GOOGLE_NEWS_LOCALES: dict[str, tuple[str, str, str]] = {"vi-VN": ("vi", "VN", "VN:vi"), "en-US": ("en", "US", "US:en")}
_QUERY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SITE_OPERATOR_RE = re.compile(r"(?i)\bsite\s*:")


def google_news_url(query: object, site: object, locale: object) -> str:
    """Escaped Google News RSS search URL from allowlisted scope; any other value is ``provider_scope_invalid``."""
    if not isinstance(query, str) or site not in GOOGLE_NEWS_SITES or locale not in GOOGLE_NEWS_LOCALES:
        raise ProviderPayloadError("provider_scope_invalid")
    cleaned = " ".join(_QUERY_CONTROL_RE.sub(" ", query).split())
    if not 1 <= len(cleaned) <= 200 or _SITE_OPERATOR_RE.search(cleaned):
        raise ProviderPayloadError("provider_scope_invalid")
    hl, gl, ceid = GOOGLE_NEWS_LOCALES[str(locale)]
    q = cleaned if site == "any" else f"{cleaned} site:{site}"
    return f"{GOOGLE_NEWS_URL}?" + urlencode({"q": q, "hl": hl, "gl": gl, "ceid": ceid}, quote_via=quote)


def preset_url(provider: str, configuration: Mapping[str, Any] | None = None) -> str:
    """Fixed URL for a feed preset, or the server-built Google News URL from stored scope."""
    if provider == "google_news":
        cfg = configuration or {}
        return google_news_url(cfg.get("news_query"), cfg.get("news_site"), cfg.get("news_locale"))
    url = FEED_URLS.get(provider)
    if url is None:
        raise ProviderPayloadError("provider_scope_invalid")
    return url

HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_MAX_ITEMS = 10  # items fetched per run; one extra call lists the ids (11 sends total)
_HN_MAX_BODY = 64 * 1024


# --------------------------------------------------------------------------- shared text/url/time policy

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        elif tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def plain_text(value: str | None, limit: int) -> str:
    """Display-safe text: markup and active content dropped, control chars removed, whitespace folded."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value[: limit * 4])
    parser.close()
    return " ".join(_CONTROL_RE.sub(" ", "".join(parser.parts)).split())[:limit]


def https_link(raw: object) -> str | None:
    """A citation URL: https, host, no credentials, no whitespace/control chars; else None."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", raw):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return raw


def parse_when(raw: str | None, collected_at: datetime) -> datetime | None:
    """RFC 2822 or ISO 8601 with an explicit zone -> UTC; naive, absurd or future (>10 min) -> None."""
    if not raw or len(raw) > 64:
        return None
    raw = raw.strip()
    try:
        moment = datetime.fromisoformat(raw) if re.match(r"\d{4}-\d{2}-\d{2}", raw) else parsedate_to_datetime(raw)
    except (ValueError, TypeError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    moment = moment.astimezone(UTC)
    if moment.year < 2000 or (moment - collected_at).total_seconds() > 600:
        return None
    return moment


def _stable_id(raw: str) -> str:
    return raw if len(raw) <= 480 else "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _license_label(provider: str) -> str | None:
    if provider == "google_news":
        return GOOGLE_NEWS_LICENSE
    spec = get_provider_spec(provider)
    return spec.attribution[:255] if spec else None


def _record(
    *, provider: str, identity: str, title: str, summary: str, observed_at: datetime, collected_at: datetime,
    version: str, basis: Literal["provider_modified", "provider_published", "collection"],
    modified_at: datetime | None, coverage: Literal["returned_snapshot", "truncated"],
    source_fields: dict[str, Any], canonical_url: str | None, published_at: str | None = None,
    provider_updated_at: str | None = None,
) -> IngestionRecord:
    from modules.knowledge.documents.schemas import ProviderRecordMetadata

    envelope = ProviderRecordMetadata(
        provider=provider, identity=identity, provider_version=version,
        timestamp_basis=basis, coverage=coverage, content_truncated=False,
        provider_modified_at=modified_at, license_label=_license_label(provider), source_fields=source_fields,
    )
    metadata: dict[str, Any] = {
        "title": title, "provider_record": envelope.model_dump(mode="json"),
        "content_scope": "feed_summary", "full_text_available": False,
    }
    if canonical_url:
        metadata["canonical_url"] = canonical_url
    if published_at:
        metadata["published_at"] = published_at
    if provider_updated_at:
        metadata["provider_updated_at"] = provider_updated_at
    return IngestionRecord(
        provider_id=identity, content=(title + (f"\n\n{summary}" if summary else ""))[:_MAX_SUMMARY + _MAX_TITLE],
        observed_at=observed_at, collected_at=collected_at, version=version, metadata=metadata,
    )


# --------------------------------------------------------------------------- RSS / Atom

def safe_xml_root(body: object) -> ET.Element:
    """Parse bounded, strictly UTF-8 XML; DOCTYPE/ENTITY (and other encodings that could hide them) are refused."""
    if not isinstance(body, (bytes, bytearray)) or not body:
        raise ProviderPayloadError("news_feed_invalid")
    if len(body) > MAX_FEED_BYTES:
        raise ProviderPayloadError("news_feed_too_large")
    try:
        text = bytes(body).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderPayloadError("news_feed_xml_malformed") from exc
    declared = re.match(r"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", text)
    if declared and declared.group(1).lower() != "utf-8":
        raise ProviderPayloadError("news_feed_xml_malformed")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ProviderPayloadError("news_feed_forbidden_xml_construct")
    try:
        return ET.fromstring(text.encode("utf-8"))  # hostile constructs rejected above
    except ET.ParseError as exc:
        raise ProviderPayloadError("news_feed_xml_malformed") from exc


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el: ET.Element, *names: str) -> str | None:
    for child in el:
        if _local(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _atom_link(el: ET.Element) -> str | None:
    best = None
    for child in el:
        if _local(child.tag) == "link" and child.attrib.get("rel", "alternate") == "alternate":
            best = best or child.attrib.get("href")
    return best


def _scan_items(root: ET.Element) -> list[ET.Element]:
    kind = _local(root.tag)
    if kind == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is None:
            raise ProviderPayloadError("news_feed_invalid")
        items = [c for c in channel if _local(c.tag) == "item"]
    elif kind == "feed":
        items = [c for c in root if _local(c.tag) == "entry"]
    else:
        raise ProviderPayloadError("news_feed_invalid")
    if len(items) > MAX_FEED_ITEMS_SCANNED:
        raise ProviderPayloadError("news_feed_too_many_items")
    return items


def map_feed(provider: str, body: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Map one RSS 2.0 / Atom body for a fixed preset; order-independent, no fetch of linked articles."""
    if provider not in FEED_URLS and provider != "google_news":
        raise ProviderPayloadError("provider_scope_invalid")
    items = _scan_items(safe_xml_root(body))
    by_id: dict[str, tuple[tuple[datetime, str], IngestionRecord]] = {}
    for item in items:
        atom = _local(item.tag) == "entry"
        link = https_link(_atom_link(item) if atom else _child_text(item, "link"))
        guid = _child_text(item, "id" if atom else "guid")
        if guid is not None and (len(guid) > 2048 or _CONTROL_RE.search(guid)):
            guid = None
        raw_id = guid or link
        title = plain_text(_child_text(item, "title"), _MAX_TITLE)
        if raw_id is None or not title:
            continue  # nothing stable to key on, or nothing to show
        identity = _stable_id(raw_id)
        published = parse_when(_child_text(item, "published" if atom else "pubDate", "date"), collected_at)
        updated = parse_when(_child_text(item, "updated") if atom else None, collected_at)
        summary = plain_text(_child_text(item, "summary", "content") if atom else _child_text(item, "description"), _MAX_SUMMARY)
        # Google News items name the original publisher in <source>; the item link is kept as the citation.
        publisher = plain_text(_child_text(item, "source"), 200) or None if provider == "google_news" else PUBLISHERS[provider]
        pub_iso = published.isoformat() if published else None
        upd_iso = updated.isoformat() if updated else None
        fields: dict[str, Any] = {"title": title, "guid": identity if guid else None, "publisher": publisher}
        if summary:
            fields["summary"] = summary
        if link:
            fields["canonical_url"] = link
        if pub_iso:
            fields["published_at"] = pub_iso
        if upd_iso:
            fields["provider_updated_at"] = upd_iso
        fields = {k: v for k, v in fields.items() if v is not None}
        observed = published or updated or collected_at
        basis: Literal["provider_modified", "provider_published", "collection"] = (
            "provider_published" if published else "provider_modified" if updated else "collection"
        )
        version = content_version(provider, identity, title, summary, link, pub_iso, upd_iso)
        record = _record(
            provider=provider, identity=identity, title=title, summary=summary, observed_at=observed,
            collected_at=collected_at, version=version, basis=basis, modified_at=updated,
            coverage="returned_snapshot", source_fields=fields, canonical_url=link,
            published_at=pub_iso, provider_updated_at=upd_iso,
        )
        key = (published or updated or datetime.min.replace(tzinfo=UTC), identity)
        old = by_id.get(identity)
        if old is None or (record.version or "") < (old[1].version or ""):  # reorder-proof duplicate pick
            by_id[identity] = (key, record)
    ranked = sorted(by_id.values(), key=lambda pair: (pair[0][0], pair[0][1]), reverse=True)
    if not ranked:
        raise ProviderPayloadError("news_feed_no_items", "incomplete")
    truncated = len(ranked) > MAX_FEED_ITEMS
    records = [rec for _, rec in ranked[:MAX_FEED_ITEMS]]
    if truncated:
        records = [rec.model_copy(update={"metadata": _with_coverage(rec.metadata)}) for rec in records]
    return records


def _with_coverage(metadata: dict[str, Any]) -> dict[str, Any]:
    out = dict(metadata)
    record = dict(out["provider_record"])
    record["coverage"] = "truncated"
    out["provider_record"] = record
    return out


# --------------------------------------------------------------------------- conditional GET

@dataclass(frozen=True)
class FeedValidators:
    """ETag/Last-Modified bound to the exact URL and config revision they were learned for."""
    url: str
    config_revision: str
    etag: str | None = None
    last_modified: str | None = None


def _header_value(raw: str | None) -> str | None:
    if raw is None or not raw.strip() or len(raw) > 256 or re.search(r"[\x00-\x1f\x7f]", raw):
        return None
    return raw.strip()


def feed_request(
    provider: str, stored: FeedValidators | None = None, config_revision: str = "", url: str | None = None,
) -> ProviderRequest:
    """Preset GET (``url`` is the server-built Google News URL); validators replay only when URL and revision match."""
    url = url or preset_url(provider)
    headers: dict[str, str] = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9"}
    if stored is not None and stored.url == url and stored.config_revision == config_revision:
        if _header_value(stored.etag):
            headers["If-None-Match"] = str(_header_value(stored.etag))
        if _header_value(stored.last_modified):
            headers["If-Modified-Since"] = str(_header_value(stored.last_modified))
    return ProviderRequest("GET", url, headers)


def capture_validators(
    provider: str, headers: Mapping[str, str], config_revision: str = "", url: str | None = None,
) -> FeedValidators | None:
    """Validators from a 200 response, tied to this preset URL and revision; None when the feed offers none."""
    lowered = {k.lower(): v for k, v in headers.items()}
    etag, modified = _header_value(lowered.get("etag")), _header_value(lowered.get("last-modified"))
    if not etag and not modified:
        return None
    return FeedValidators(url or preset_url(provider), config_revision, etag, modified)


@dataclass(frozen=True)
class FeedResult:
    records: tuple[IngestionRecord, ...]
    not_modified: bool = False


def map_feed_response(provider: str, status: int, body: bytes, collected_at: datetime) -> FeedResult:
    """304 keeps the last good records (no empty batch); 200 maps; anything else is not a feed."""
    if status == 304:
        return FeedResult((), not_modified=True)
    if status != 200:
        raise ProviderPayloadError("news_feed_status_invalid")
    return FeedResult(tuple(map_feed(provider, body, collected_at)))


# --------------------------------------------------------------------------- Hacker News seam

def hn_top_request() -> ProviderRequest:
    return ProviderRequest("GET", HN_TOP_URL)


def hn_item_request(item_id: int) -> ProviderRequest:
    if type(item_id) is not int or item_id <= 0:
        raise ProviderPayloadError("hn_item_id_invalid")
    return ProviderRequest("GET", HN_ITEM_URL.format(id=item_id))


def parse_hn_top_ids(body: bytes) -> tuple[int, ...]:
    """First ``HN_MAX_ITEMS`` distinct positive ids, in provider rank order; anything else is an error."""
    if len(body) > _HN_MAX_BODY:
        raise ProviderPayloadError("provider_response_too_large")
    payload = decode_json(body)
    if not isinstance(payload, list) or not payload:
        raise ProviderPayloadError("hn_top_response_invalid")
    ids: list[int] = []
    for raw in payload[: HN_MAX_ITEMS * 4]:
        if type(raw) is not int or raw <= 0:
            raise ProviderPayloadError("hn_top_response_invalid")
        if raw not in ids:
            ids.append(raw)
        if len(ids) == HN_MAX_ITEMS:
            break
    return tuple(ids)


@dataclass(frozen=True)
class HnResult:
    records: tuple[IngestionRecord, ...]
    skipped: dict[str, int] = field(default_factory=dict)


def _hn_skip_reason(item: object) -> str | None:
    if not isinstance(item, dict):
        return "missing"
    if item.get("deleted") is True:
        return "deleted"
    if item.get("dead") is True:
        return "dead"
    if item.get("type") != "story":
        return "not_story"
    return None


def map_hn_stories(ids: tuple[int, ...], bodies: Mapping[int, bytes | None], collected_at: datetime) -> HnResult:
    """Map fetched items (``None`` body = not fetched/404). Removed/dead/non-story items are skipped and counted.

    The article ``url`` is kept as a citation only; Ask-HN style stories without one keep their own text.
    Score/comment counts are volatile and excluded from the version.
    """
    if len(ids) > HN_MAX_ITEMS:
        raise ProviderPayloadError("hn_fanout_exceeded")
    skipped: dict[str, int] = {}
    records: list[IngestionRecord] = []
    for item_id in ids:
        raw = bodies.get(item_id)
        item = decode_json(raw) if raw is not None else None
        reason = _hn_skip_reason(item)
        if reason is None and (not isinstance(item, dict) or item.get("id") != item_id):
            reason = "id_mismatch"
        if reason is not None or not isinstance(item, dict):
            skipped[reason or "missing"] = skipped.get(reason or "missing", 0) + 1
            continue
        title = plain_text(item.get("title") if isinstance(item.get("title"), str) else None, _MAX_TITLE)
        if not title:
            skipped["no_title"] = skipped.get("no_title", 0) + 1
            continue
        url = https_link(item.get("url"))
        text = plain_text(item.get("text") if isinstance(item.get("text"), str) else None, _MAX_SUMMARY)
        by = item.get("by") if isinstance(item.get("by"), str) and len(item["by"]) <= 128 else None
        when = item.get("time")
        posted = datetime.fromtimestamp(when, UTC) if type(when) is int and 946684800 <= when <= collected_at.timestamp() + 600 else None
        fields: dict[str, Any] = {"title": title, "id": item_id, "type": "story"}
        for key, value in (("url", url), ("text", text or None), ("by", by),
                           ("time", int(posted.timestamp()) if posted else None)):
            if value is not None:
                fields[key] = value
        identity = f"hn:{item_id}"
        records.append(_record(
            provider="hn_top", identity=identity, title=title, summary=text, observed_at=posted or collected_at,
            collected_at=collected_at, version=content_version("hn_top", item_id, title, url, text, by),
            basis="provider_published" if posted else "collection", modified_at=None,
            coverage="returned_snapshot", source_fields=fields, canonical_url=url,
            published_at=posted.isoformat() if posted else None,
        ))
    return HnResult(tuple(records), skipped)


# --------------------------------------------------------------------------- registry

def _feed_mapper(provider: str) -> Callable[[Any, datetime], list[IngestionRecord]]:
    return lambda body, collected_at: map_feed(provider, body, collected_at)


def news_adapters() -> dict[str, PureAdapter]:
    """Feed presets plus experimental GDELT (imported lazily: gdelt.py reuses this module's helpers)."""
    from modules.connectors.providers.gdelt import GDELT_ADAPTER

    return {
        **{p: PureAdapter(lambda p=p: feed_request(p), _feed_mapper(p), binary=True) for p in FEED_URLS},
        "gdelt_economy": GDELT_ADAPTER,
    }


def map_news_body(provider: str, body: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Single-request news providers (feeds, GDELT). HN needs the explicit fan-out seam above."""
    adapter = news_adapters().get(provider)
    if adapter is None:
        raise ProviderPayloadError("provider_scope_invalid")
    return adapter.mapper(body if adapter.binary else decode_json(body), collected_at)
