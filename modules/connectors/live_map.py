"""Fixed-endpoint live map provider adapters; payloads are never persisted."""

import asyncio
import csv
import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.heavy_work import to_thread_joined
from modules.connectors import quota
from modules.connectors.live_map_catalog import CATALOG_IDS
from modules.connectors.live_map_catalog import (
    live_cache_seconds as live_cache_seconds,
)
from modules.connectors.providers import news, rest
from modules.connectors.providers.feed_catalog import _retry_deadline
from modules.connectors.public import ProviderRateLimited

QuotaDeferred = quota.QuotaDeferred
CollectionIncomplete = rest.CollectionIncomplete
UnsafeDestination = rest.UnsafeDestination
ProviderHttpError = rest.ProviderHttpError

MAX_ITEMS = 100
MAX_BYTES = 4 * 1024 * 1024
PROVIDERS = frozenset(
    {
        "firms",
        "lobsters",
        "cisa_kev",
        "business",
        "fed",
        "bbc_world",
        "aljazeera",
        "vnexpress_world",
        "vnexpress_news",
        "gaming_news",
        "sec",
    }
)
SOURCE_NAMES = {
    "firms": "NASA FIRMS",
    "lobsters": "Lobsters",
    "cisa_kev": "CISA KEV",
    "business": "VnExpress",
    "fed": "Federal Reserve",
    "bbc_world": "BBC News",
    "aljazeera": "Al Jazeera",
    "vnexpress_world": "VnExpress",
    "vnexpress_news": "VnExpress",
    "gaming_news": "Eurogamer",
    "sec": "U.S. SEC EDGAR",
}


async def debit_live_send(session: AsyncSession, *, provider: str, workspace_id: UUID) -> None:
    """Debit a rotation-stable instance-global live budget without retaining keys or provider content."""
    del workspace_id
    await quota.debit_transient_send(session, provider_id=CATALOG_IDS[provider])


async def defer_live_provider(session: AsyncSession, *, provider: str, until: datetime) -> None:
    """Persist a provider retry deadline for the rotation-stable instance-global subject."""
    await quota.defer_provider(session, provider_id=CATALOG_IDS[provider], until=until)


def _text(value: Any, limit: int = 500) -> str | None:
    """Return bounded plain text or None for missing/non-string provider values."""
    return " ".join(value.split())[:limit] or None if isinstance(value, str) else None


def _url(value: Any) -> str | None:
    """Expose only bounded HTTPS links with a host and no embedded credentials."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        p = urlsplit(value)
        return (
            value
            if p.scheme == "https" and p.hostname and not p.username and not p.password
            else None
        )
    except ValueError:
        return None


def _timestamp(value: Any) -> str | None:
    """Normalize provider timestamps to UTC ISO 8601; leave unparseable values unknown."""
    try:
        if isinstance(value, (int, float)):
            result = datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, UTC)
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value)
            except ValueError:
                try:
                    result = datetime.strptime(value, "%Y-%m-%d %H%M UTC").replace(tzinfo=UTC)
                except ValueError:
                    result = parsedate_to_datetime(value)
        else:
            return None
        return (
            result.replace(tzinfo=UTC).isoformat()
            if result.tzinfo is None
            else result.astimezone(UTC).isoformat()
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _item(
    provider: str,
    identity: Any,
    title: Any,
    observed: Any,
    *,
    lat: Any = None,
    lon: Any = None,
    url: Any = None,
    summary: Any = None,
) -> dict[str, Any] | None:
    """Project a provider record to a bounded display-only item with valid coordinates."""
    title = _text(title)
    if not title or identity is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat, lon = None, None
    except (ValueError, TypeError):
        lat, lon = None, None
    return {
        "id": str(identity)[:255],
        "category": provider,
        "title": title,
        "observed_at": _timestamp(observed),
        "latitude": lat,
        "longitude": lon,
        "url": _url(url),
        "source": SOURCE_NAMES[provider],
        "summary": _text(summary, 1000),
        "sources": [],
    }


def _endpoint(
    provider: str,
    country: str | None,
    owner: str | None,
    repo: str | None,
    cik: str | None,
    secret: str | None,
) -> str:
    """Construct a fixed-origin endpoint after validating every path component."""
    endpoints = {
        "lobsters": "https://lobste.rs/rss",
        "cisa_kev": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "business": "https://vnexpress.net/rss/kinh-doanh.rss",
        "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
        "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "vnexpress_world": "https://vnexpress.net/rss/the-gioi.rss",
        "vnexpress_news": "https://vnexpress.net/rss/thoi-su.rss",
        # polygon.com resets connections from non-browser clients; Eurogamer serves the same topic.
        "gaming_news": "https://www.eurogamer.net/feed",
    }
    if provider in endpoints:
        return endpoints[provider]
    if provider == "sec" and cik and re.fullmatch(r"[0-9]{1,10}", cik):
        return f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    if provider == "firms" and secret and country and re.fullmatch(r"[A-Z]{3}", country):
        return f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/{quote(secret, safe='')}/VIIRS_SNPP_NRT/{country}/1"
    raise ValueError("provider_scope_invalid")


def _parse_xml_items(provider: str, body: bytes) -> tuple[Any, list[Any]]:
    """Parse an RSS/Atom provider (business, fed, lobsters, world/news/gaming feeds) into ``(rows, records)``."""
    root = news.safe_xml_root(body, max_bytes=MAX_BYTES)
    rows = news._scan_items(root)
    records = []
    for r in rows:
        link = news._atom_link(r) if news._local(r.tag) == "entry" else news._child_text(r, "link")
        records.append(
            _item(
                provider,
                news._child_text(r, "guid", "id") or link,
                news._child_text(r, "title"),
                news._child_text(r, "pubDate", "published", "updated"),
                url=link,
                summary=news._child_text(r, "description", "summary"),
            )
        )
    return rows, records


def _parse_firms(provider: str, body: bytes) -> tuple[Any, list[Any]]:
    """Parse the NASA FIRMS CSV into ``(rows, records)`` of unconfirmed thermal detections."""
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    records = [
        _item(
            provider,
            f"{r.get('latitude')}:{r.get('longitude')}:{r.get('acq_date')}:{r.get('acq_time')}",
            "Thermal detection (not a confirmed fire)",
            f"{r.get('acq_date', '')} {r.get('acq_time', '').zfill(4)} UTC",
            lat=r.get("latitude"),
            lon=r.get("longitude"),
            summary=f"Satellite: {r.get('satellite', '')}",
        )
        for r in rows
    ]
    return rows, records


def _parse_sec(provider: str, data: Any) -> tuple[Any, list[Any]]:
    """Map the SEC submissions index into ``(rows, records)`` of recent filings."""
    recent = data["filings"]["recent"]
    rows = recent["accessionNumber"]
    records = []
    for i, accession in enumerate(rows):
        form = recent["form"][i]
        records.append(
            _item(
                provider,
                accession,
                f"{data.get('name', 'SEC filing')}: {form}",
                recent["filingDate"][i],
                url=f"https://www.sec.gov/Archives/edgar/data/{int(data['cik'])}/{accession.replace('-', '')}/{recent['primaryDocument'][i]}",
            )
        )
    return rows, records


def _parse_cisa_kev(provider: str, data: Any) -> tuple[Any, list[Any]]:
    """Map the CISA KEV catalog into ``(rows, records)``, newest additions first.

    The catalog is not guaranteed to arrive newest-first; sorting keeps recent additions inside the cap.
    """
    rows = sorted(
        data["vulnerabilities"],
        key=lambda row: row.get("dateAdded", "") if isinstance(row, dict) else "",
        reverse=True,
    )
    records = [
        _item(
            provider,
            r.get("cveID"),
            r.get("vulnerabilityName"),
            r.get("dateAdded"),
            url=f"https://nvd.nist.gov/vuln/detail/{r.get('cveID')}",
            summary=r.get("shortDescription"),
        )
        for r in rows
    ]
    return rows, records


# Provider -> JSON mapper for the providers that return JSON.
_JSON_PARSERS: dict[str, Callable[[str, Any], tuple[Any, list[Any]]]] = {
    "cisa_kev": _parse_cisa_kev,
    "sec": _parse_sec,
}

# Providers served as RSS/Atom XML.
_XML_PROVIDERS = frozenset(
    {
        "business",
        "fed",
        "lobsters",
        "bbc_world",
        "aljazeera",
        "vnexpress_world",
        "vnexpress_news",
        "gaming_news",
    }
)


def _parse(provider: str, body: bytes) -> tuple[list[dict[str, Any]], bool]:
    """Parse fixed provider formats and return at most 100 bounded display records.

    Pure and thread-safe (runs off the event loop). Malformed rows raise ``KeyError`` /
    ``IndexError`` / ``TypeError``, which ``fetch_events`` maps to ``provider_response_invalid``.
    ``_item`` construction is not cut short at ``MAX_ITEMS``.
    """
    if provider in _XML_PROVIDERS:
        rows, records = _parse_xml_items(provider, body)
    elif provider == "firms":
        rows, records = _parse_firms(provider, body)
    else:
        rows, records = _JSON_PARSERS[provider](provider, json.loads(body))
    return [r for r in records[:MAX_ITEMS] if r is not None], len(rows) > MAX_ITEMS


async def fetch_events(
    provider: str,
    *,
    country: str | None,
    owner: str | None,
    repo: str | None,
    cik: str | None,
    secret: str | None,
    contact: str | None,
    before_send: rest.BeforeSend,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch one allowlisted provider through pinned, bounded egress without logging payloads."""
    if provider not in PROVIDERS:
        raise ValueError("provider_invalid")
    url = _endpoint(provider, country, owner, repo, cik, secret)
    headers = {"User-Agent": f"Umwelt-OS {contact}"} if provider == "sec" and contact else {}
    if provider == "sec" and not contact:
        raise ValueError("contact_required")
    body: bytes | None
    if provider == "firms":
        body = await _secret_fetch(url, before_send=before_send, max_bytes=MAX_BYTES)
    else:
        response = await rest.fetch_bounded(
            url, headers=headers, before_send=before_send, max_bytes=MAX_BYTES, timeout=15
        )
        body = response.body
    if body is None:
        raise ValueError("provider_empty_response")
    try:
        # CSV/JSON/XML parse + _item() for up to 4 MB runs on the API loop otherwise (A2). Exceptions
        # raised in the thread propagate unchanged, so the KeyError/IndexError/TypeError mapping holds.
        # _item() construction is deliberately NOT cut short at MAX_ITEMS: a malformed row past the
        # cap currently raises provider_response_invalid, which an early stop would silently change.
        return await to_thread_joined(_parse, provider, body)
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("provider_response_invalid") from exc


async def _secret_fetch(url: str, *, before_send: rest.BeforeSend, max_bytes: int) -> bytes:
    """Use pinned transport without HTTPX client URL logging for provider secrets in paths or queries."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise rest.UnsafeDestination("url_not_allowed")
    addresses = await rest.resolve_public(parts.hostname, 443)
    pinned, headers, extensions = rest._pinned_request(
        url,
        addresses[0],
        {
            "User-Agent": rest.DEFAULT_USER_AGENT,
            "Accept-Encoding": "identity",
        },
    )
    extensions["timeout"] = {"connect": 15, "read": 15, "write": 15, "pool": 15}
    request = httpx.Request("GET", pinned, headers=headers, extensions=extensions)
    # AsyncClient logs complete URLs at INFO; the direct transport does not log the URL.
    async with asyncio.timeout(15), httpx.AsyncHTTPTransport(trust_env=False) as transport:
        await before_send()
        response = await transport.handle_async_request(request)
        try:
            if response.status_code == 429:
                raise ProviderRateLimited(_retry_deadline(response.headers, datetime.now(UTC)))
            if 300 <= response.status_code < 400:
                raise rest.UnsafeDestination("redirect_blocked")
            if response.status_code >= 400:
                raise rest.ProviderHttpError(response.status_code)
            return await rest._read_capped(response, max_bytes)
        finally:
            await response.aclose()


async def fetch_weather(
    layer: str, z: int, x: int, y: int, key: str, *, before_send: rest.BeforeSend
) -> bytes:
    """Retrieve one validated OpenWeather PNG tile without exposing the key to clients or HTTP logs."""
    layers = {
        "temperature": "temp_new",
        "precipitation": "precipitation_new",
        "wind": "wind_new",
        "clouds": "clouds_new",
    }
    if layer not in layers or not 0 <= z <= 18 or not 0 <= x < 2**z or not 0 <= y < 2**z or not key:
        raise ValueError("weather_tile_invalid")
    url = f"https://tile.openweathermap.org/map/{layers[layer]}/{z}/{x}/{y}.png?{urlencode({'appid': key})}"
    body = await _secret_fetch(url, before_send=before_send, max_bytes=1024 * 1024)
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("weather_response_invalid")
    return body
