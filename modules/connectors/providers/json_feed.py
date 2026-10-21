"""Table-driven JSON list/event feeds: one declarative ``JsonFeed`` row per fixed public endpoint.

No network and no database. A row names the endpoint, the dotted path to its item list and the dotted
paths of each item's id/title/summary/time/link fields; ``map_json_feed`` turns an already-fetched body
into news-shaped ``IngestionRecord`` envelopes (feed title/summary/link, no typed measurement) through
the same ``news._record`` policy the RSS presets use. Adding a feed is a table row plus a spec entry.

Conventions
- Paths are dotted (``properties.name``); an integer segment indexes a list; ``""`` is the root.
- An item missing its id or title is skipped; a missing/non-list item path is ``<provider>_response_invalid``
  so a changed upstream shape fails loudly instead of yielding an empty "success".
- Coordinates, when declared, are range-checked and written into the summary text only.
- ``observed_at`` is the parsed item time, else ``collected_at`` with ``timestamp_basis="collection"``;
  the version never includes the collection clock.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    content_version,
    decode_json,
)
from modules.connectors.providers.news import (
    _MAX_SUMMARY,
    _MAX_TITLE,
    MAX_FEED_ITEMS,
    _record,
    _stable_id,
    _with_coverage,
    https_link,
    parse_when,
    plain_text,
)
from modules.ingestion.schemas import IngestionRecord

MAX_ROWS_SCANNED = 2000  # sanity bound on the decoded list; real feeds here return tens to a few hundred rows
_TZ_SUFFIX_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")
_MISSING = object()


@dataclass(frozen=True)
class JsonFeed:
    """Declarative description of one fixed JSON endpoint and how its items become records.

    ``url`` is a fixed https URL or a function of the collection instant (date-addressed endpoints).
    ``summary`` is a tuple of ``(label, path)`` pairs rendered as ``Label: value`` lines.
    """

    provider: str
    publisher: str
    url: str | Callable[[datetime], str]
    items: str
    id: tuple[str, ...]
    title: tuple[str, ...]
    summary: tuple[tuple[str, str], ...] = ()
    time: str | None = None
    link: str | None = None
    link_template: str = "{}"
    lonlat: tuple[str, str] | None = None
    naive_utc: bool = False  # provider emits zone-less ISO times that are documented as UTC
    underscore_titles: bool = False  # Wikipedia article keys use ``_`` for spaces
    skip_id_prefixes: tuple[str, ...] = ()  # non-content pages (Main_Page, Special:...) that top lists include
    accept: str = "application/json"


def _get(node: Any, path: str) -> Any:
    """Follow a dotted path through dicts (key) and lists (integer segment); ``_MISSING`` when absent."""
    if not path:
        return node
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part, _MISSING)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
        if node is _MISSING:
            return _MISSING
    return node


def _text(node: Any, path: str, limit: int = 300) -> str | None:
    """A scalar (str/int/Decimal, never bool/None) at ``path`` as bounded printable text, else None."""
    value = _get(node, path)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    cleaned = plain_text(str(value), limit)
    return cleaned or None


def _coordinate(node: Any, path: str, bound: int) -> str | None:
    """Range-checked coordinate text, or None when absent or outside +/-``bound``."""
    raw = _get(node, path)
    try:
        number = float(raw)  # Decimal/int/float/str convert; anything else raises
    except (TypeError, ValueError):
        return None
    return f"{number:.4f}".rstrip("0").rstrip(".") if abs(number) <= bound else None


def feed_request(feed: JsonFeed) -> ProviderRequest:
    """Fixed GET for a feed row; date-addressed rows resolve against the current UTC day."""
    url = feed.url(datetime.now(UTC)) if callable(feed.url) else feed.url
    return ProviderRequest("GET", url, {"Accept": feed.accept})


def _item_time(feed: JsonFeed, item: Any, collected_at: datetime) -> datetime | None:
    """Parsed item instant (UTC) per the row's time path; ``naive_utc`` rows get an explicit zone first."""
    raw = _text(item, feed.time, 64) if feed.time else None
    if raw is None:
        return None
    if feed.naive_utc and not _TZ_SUFFIX_RE.search(raw):
        raw += "+00:00"
    return parse_when(raw, collected_at)


def map_json_feed(feed: JsonFeed, body: bytes, collected_at: datetime) -> list[IngestionRecord]:
    """Map one fetched JSON body to <= ``MAX_FEED_ITEMS`` news-shaped records, newest first.

    Raises ``ProviderPayloadError``: oversize or non-JSON body, item path missing or not a list, or no
    usable item (kind ``incomplete``). Malformed individual items are skipped; a list longer than the cap
    is flagged ``truncated`` coverage. Pure; identity is the declared id parts, not list position.
    """
    rows = _get(decode_json(body), feed.items)  # decode_json enforces the 2 MiB body bound
    if not isinstance(rows, list) or len(rows) > MAX_ROWS_SCANNED:
        raise ProviderPayloadError(f"{feed.provider}_response_invalid")
    by_id: dict[str, tuple[datetime, IngestionRecord]] = {}
    for item in rows:
        if not isinstance(item, (dict, list)):
            continue
        id_parts = [_text(item, path, 200) for path in feed.id]
        title_parts = [_text(item, path, _MAX_TITLE) for path in feed.title]
        raw_id = ":".join(p for p in id_parts if p) if all(id_parts) else ""
        title = " - ".join(p for p in title_parts if p)[:_MAX_TITLE]
        if not raw_id or not title or any(raw_id.startswith(prefix) for prefix in feed.skip_id_prefixes):
            continue
        if feed.underscore_titles:
            title = title.replace("_", " ")
        lines = [f"{label}: {value}" for label, path in feed.summary if (value := _text(item, path, 400))]
        if feed.lonlat:
            lon, lat = _coordinate(item, feed.lonlat[0], 180), _coordinate(item, feed.lonlat[1], 90)
            if lon is not None and lat is not None:
                lines.append(f"Location: {lat}, {lon}")
        summary = plain_text("; ".join(lines), _MAX_SUMMARY)
        link_key = _text(item, feed.link, 400) if feed.link else None
        link = https_link(feed.link_template.format(quote(link_key, safe=":/?=&%#._-")) if link_key else None)
        identity = _stable_id(f"{feed.provider}:{raw_id}")
        when = _item_time(feed, item, collected_at)
        iso = when.isoformat() if when else None
        fields: dict[str, Any] = {"title": title, "guid": identity, "publisher": feed.publisher}
        for key, value in (("summary", summary or None), ("canonical_url", link), ("published_at", iso)):
            if value:
                fields[key] = value
        record = _record(
            provider=feed.provider, identity=identity, title=title, summary=summary,
            observed_at=when or collected_at, collected_at=collected_at,
            version=content_version(feed.provider, identity, title, summary, link, iso),
            basis="provider_published" if when else "collection", modified_at=None,
            coverage="returned_snapshot", source_fields=fields, canonical_url=link, published_at=iso,
        )
        old = by_id.get(identity)
        if old is None or (record.version or "") < (old[1].version or ""):  # reorder-proof duplicate pick
            by_id[identity] = (when or datetime.min.replace(tzinfo=UTC), record)
    if not by_id:
        raise ProviderPayloadError(f"{feed.provider}_no_items", "incomplete")
    ranked = sorted(by_id.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)
    records = [pair[1][1] for pair in ranked[:MAX_FEED_ITEMS]]
    if len(ranked) > MAX_FEED_ITEMS:
        records = [rec.model_copy(update={"metadata": _with_coverage(rec.metadata)}) for rec in records]
    return records


def _wiki_day(template: str) -> Callable[[datetime], str]:
    """URL function for date-addressed Wikimedia endpoints: yesterday (UTC), the latest complete day."""
    def build(now: datetime) -> str:
        """Fill ``{y}/{m}/{d}`` with the previous UTC calendar day."""
        day = now - timedelta(days=1)
        return template.format(y=f"{day.year:04d}", m=f"{day.month:02d}", d=f"{day.day:02d}")
    return build


_WIKI_SKIP = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "Help:", "File:", "Category:", "Talk:", "Template:")
_WIKI_LINK = "https://en.wikipedia.org/wiki/{}"

# Endpoint shapes were checked against live responses on 2026-10-21 (see docs/free-data-provider-policy.md).
# Dropped after the same probe: ransomware_live (documented v2 paths 404 without a paid key) and safecast
# (the public measurements list ignores its ordering parameter and returns years-old rows).
JSON_FEEDS: dict[str, JsonFeed] = {f.provider: f for f in (
    JsonFeed(
        "gdacs_events", "GDACS",
        "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ;TC;FL;VO;WF;DR&alertlevel=Orange;Red",
        "features", ("properties.eventtype", "properties.eventid"), ("properties.name",),
        (("Alert", "properties.alertlevel"), ("Countries", "properties.country"),
         ("Severity", "properties.severitydata.severitytext"), ("From", "properties.fromdate")),
        time="properties.datemodified", link="properties.url.report", naive_utc=True,
        lonlat=("geometry.coordinates.0", "geometry.coordinates.1"),
    ),
    JsonFeed(
        "nws_alerts", "U.S. National Weather Service",
        "https://api.weather.gov/alerts/active?status=actual&message_type=alert&severity=Extreme,Severe",
        "features", ("properties.id",), ("properties.headline",),
        (("Area", "properties.areaDesc"), ("Severity", "properties.severity"), ("Urgency", "properties.urgency"),
         ("Ends", "properties.ends")),
        time="properties.sent", link="properties.@id", accept="application/geo+json",
    ),
    JsonFeed(
        "emsc_quakes", "EMSC",
        "https://www.seismicportal.eu/fdsnws/event/1/query?limit=50&format=json&minmag=4",
        "features", ("id",), ("properties.flynn_region",),
        (("Magnitude", "properties.mag"), ("Depth km", "properties.depth"), ("Time", "properties.time")),
        time="properties.lastupdate", lonlat=("properties.lon", "properties.lat"),
    ),
    JsonFeed(
        "launch_library", "The Space Devs Launch Library",
        "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=10&mode=list",
        "results", ("id",), ("name",),
        (("Status", "status.name"), ("NET", "net"), ("Provider", "lsp_name"), ("Pad", "pad"),
         ("Location", "location")),
        time="last_updated", link="url",
    ),
    JsonFeed(
        "wikipedia_mostread", "Wikipedia",
        _wiki_day("https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{y}/{m}/{d}"),
        "mostread.articles", ("title",), ("titles.normalized",),
        (("Views", "views"), ("Rank", "rank"), ("About", "description")),
        link="content_urls.desktop.page", skip_id_prefixes=_WIKI_SKIP,
    ),
    JsonFeed(
        "wikipedia_top_views", "Wikipedia",
        _wiki_day("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m}/{d}"),
        "items.0.articles", ("article",), ("article",),
        (("Views", "views"), ("Rank", "rank")),
        link="article", link_template=_WIKI_LINK, underscore_titles=True, skip_id_prefixes=_WIKI_SKIP,
    ),
    JsonFeed(
        "bmkg_quakes", "BMKG",
        "https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json",
        "Infogempa.gempa", ("DateTime", "Coordinates"), ("Wilayah",),
        (("Magnitude", "Magnitude"), ("Depth", "Kedalaman"), ("Coordinates", "Coordinates"),
         ("Tsunami potential", "Potensi")),
        time="DateTime",
    ),
    JsonFeed(
        "satnogs_observations", "SatNOGS Network",
        "https://network.satnogs.org/api/observations/?format=json&status=good",
        "", ("id",), ("tle0", "station_name"),
        (("Start", "start"), ("End", "end"), ("NORAD", "norad_cat_id"), ("Mode", "transmitter_mode"),
         ("Max elevation", "max_altitude")),
        time="start",
    ),
)}


def json_feed_adapters() -> dict[str, PureAdapter]:
    """``PureAdapter`` per row so the shared ``_run_pure`` path (gate, quota, terms) collects them."""
    def adapter(feed: JsonFeed) -> PureAdapter:
        """Bind one row into a request builder and a bytes mapper."""
        return PureAdapter(lambda: feed_request(feed), lambda body, at: map_json_feed(feed, body, at), binary=True)

    return {provider: adapter(feed) for provider, feed in JSON_FEEDS.items()}

