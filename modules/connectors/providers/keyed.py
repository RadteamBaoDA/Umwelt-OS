"""Keyed free-tier providers: owner-entered credential, one fixed endpoint, one gated send per run.

No network and no database. Each ``KeyedProvider`` row names where the owner's key travels
(``placement``), how to build the request from the key plus validated source scope, and how to map the body.
The key is stored in ``connector_rest_credentials`` with ``header_name == placement``:

- ``"<Header-Name>"``  the key rides that request header (default);
- ``"query:<param>"``  the key is appended as a query parameter;
- ``"path:<token>"``   the key replaces ``{<token>}`` in the URL path.

``SecretRequest`` hides the whole request (URL included) from ``repr`` because query/path placement puts the
key in the URL. Missing or malformed keys and scope raise ``ProviderPayloadError`` before any send.
Deferred (OAuth token exchange needs a POST the shared fetcher lacks): ACLED, OpenSky, UN Comtrade.
"""

import csv
import io
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

from modules.connectors.providers.crypto import (
    COINGECKO_KEY_HEADER,
    coingecko_request,
    map_coingecko,
)
from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    build_record,
    content_version,
    day_start,
    decimal_text,
    finite_decimal,
    parse_iso_date,
    provider_instant,
)
from modules.connectors.providers.news import (
    MAX_FEED_ITEMS,
    _record,
    _stable_id,
    https_link,
    parse_when,
    plain_text,
)
from modules.ingestion.schemas import IngestionRecord

# httpx/httpcore log full request URLs at INFO, and query/path-placed keys (see module docstring) ride in the
# URL. Pin both to WARNING once at import so a key can never reach logs (a later dictConfig can undo it).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

MAX_KEY_CHARS = 256
KEYED_MAX_BYTES = 2 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Z0-9_]{1,25}$")
_FIRMS_CAP = 200  # hotspots kept per run, strongest fire radiative power first
_NEWS_ITEM_CAP = 50


@dataclass(frozen=True, repr=False)
class SecretRequest(ProviderRequest):
    """A request whose URL and headers may carry the owner's key; ``repr`` never shows either."""

    def __repr__(self) -> str:
        """Redacted representation: method only (the URL may embed the key)."""
        return f"SecretRequest(method={self.method!r}, <redacted>)"


@dataclass(frozen=True)
class KeyedProvider:
    """One keyed preset: credential placement, request builder and body mapper (``binary`` = raw text/bytes)."""

    placement: str
    build: Callable[[str, Mapping[str, Any]], ProviderRequest]
    mapper: Callable[[Any, datetime, Mapping[str, Any]], list[IngestionRecord]]
    binary: bool = False


def validate_key(provider: str, secret: str) -> None:
    """Refuse an unusable key (empty, padded, unprintable, over-long) before it is stored or sent.

    Raises ``ProviderPayloadError`` of kind ``credential``; CoinGecko keeps its own error code.
    """
    if provider == "coingecko":
        coingecko_request(secret)
        return
    if not secret or len(secret) > MAX_KEY_CHARS or not secret.isprintable() or secret != secret.strip():
        raise ProviderPayloadError(f"{provider}_key_invalid", "credential")


def place_secret(
    provider: str, url: str, placement: str, secret: str, headers: Mapping[str, str] | None = None,
) -> SecretRequest:
    """Attach ``secret`` to ``url``/headers per ``placement`` and return the redacted request.

    ``query:`` appends one encoded parameter; ``path:`` substitutes ``{token}`` (percent-encoded, so a key can
    never add path segments); anything else is a header name. A template without its token is a code error.
    """
    validate_key(provider, secret)
    out_headers = {"Accept": "application/json", **(headers or {})}
    kind, _, name = placement.partition(":")
    if kind == "query" and name:
        url += ("&" if "?" in url else "?") + urlencode({name: secret})
    elif kind == "path" and name:
        if "{" + name + "}" not in url:
            raise ProviderPayloadError(f"{provider}_scope_invalid")
        url = url.replace("{" + name + "}", quote(secret, safe=""))
    else:
        out_headers[placement] = secret
    return SecretRequest("GET", url, out_headers)


# --------------------------------------------------------------------------- shared helpers

def _scope_text(cfg: Mapping[str, Any], key: str, pattern: re.Pattern[str], provider: str) -> str:
    """A required scope string matching ``pattern``; otherwise ``<provider>_scope_invalid``."""
    value = cfg.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProviderPayloadError(f"{provider}_scope_invalid")
    return value


def _scope_number(cfg: Mapping[str, Any], key: str, low: float, high: float, provider: str) -> float:
    """A required finite scope number within [low, high]; otherwise ``<provider>_scope_invalid``."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ProviderPayloadError(f"{provider}_scope_invalid")
    return float(value)


def _news_record(
    provider: str, publisher: str | None, ident: str, title: str, summary: str, link: str | None,
    when: datetime | None, collected_at: datetime,
) -> IngestionRecord:
    """One news-shaped record (feed title/summary/link); identity is the provider's own id or the https link."""
    identity = _stable_id(ident)
    iso = when.isoformat() if when else None
    fields: dict[str, Any] = {"title": title, "guid": identity, "publisher": publisher}
    for key, value in (("summary", summary or None), ("canonical_url", link), ("published_at", iso)):
        if value:
            fields[key] = value
    return _record(
        provider=provider, identity=identity, title=title, summary=summary, observed_at=when or collected_at,
        collected_at=collected_at, version=content_version(provider, identity, title, summary, link, iso),
        basis="provider_published" if when else "collection", modified_at=None, coverage="returned_snapshot",
        source_fields={k: v for k, v in fields.items() if v is not None}, canonical_url=link, published_at=iso,
    )


def _error_kind(code: object, message: object) -> Literal["credential", "rate_limited", "invalid"]:
    """Classify a provider error body: key problems fail the credential gate, throttling backs off."""
    text = f"{code} {message}".lower()
    if any(word in text for word in ("api key", "api_key", "apikey", "map_key", "unauthorized", "invalid key", "401")):
        return "credential"
    if any(word in text for word in ("rate", "limit", "too many", "429")):
        return "rate_limited"
    return "invalid"


# --------------------------------------------------------------------------- FRED

FRED_URL = "https://api.stlouisfed.org/fred/series/observations?file_type=json&sort_order=desc&limit=5&series_id={sid}"


def build_fred(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Latest five observations of the owner's series; the key rides the ``api_key`` query parameter."""
    sid = _scope_text(cfg, "fred_series_id", _ID_RE, "fred")
    return place_secret("fred", FRED_URL.format(sid=sid), "query:api_key", secret)


def map_fred(payload: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map observations to one measurement per date; ``.`` (FRED's missing marker) rows are skipped.

    An ``error_code`` body is classified (bad key -> credential). No numeric observation raises ``incomplete``.
    """
    sid = _scope_text(cfg, "fred_series_id", _ID_RE, "fred")
    if not isinstance(payload, dict):
        raise ProviderPayloadError("fred_response_invalid")
    if "error_code" in payload:
        raise ProviderPayloadError("fred_error_payload", _error_kind(payload.get("error_code"), payload.get("error_message")))
    rows = payload.get("observations")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ProviderPayloadError("fred_response_invalid")
    records = []
    for row in rows:
        if not isinstance(row, dict) or row.get("value") in (None, "."):
            continue
        day = parse_iso_date(row.get("date"))
        try:
            value = finite_decimal(row["value"])
        except (KeyError, ValueError) as exc:
            raise ProviderPayloadError("fred_value_invalid") from exc
        iso, text = day.isoformat(), decimal_text(value)
        records.append(build_record(
            provider="fred", identity=f"fred:{sid}:{iso}", content=f"FRED {sid} {iso}: {text}",
            title=f"FRED {sid}", observed_at=day_start(day), collected_at=collected_at,
            version=content_version("fred", sid, iso, text), metric="fred_series", value=value, unit="series units",
            symbol=sid, quality="provider_reported", provider_fields={"series_id": sid, "date": iso},
        ))
    if not records:
        raise ProviderPayloadError("fred_no_observations", "incomplete")
    return records


# --------------------------------------------------------------------------- Finnhub market news

FINNHUB_URL = "https://finnhub.io/api/v1/news?category=general"


def build_finnhub(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """General market news; the key rides the ``X-Finnhub-Token`` header."""
    return place_secret("finnhub_news", FINNHUB_URL, "X-Finnhub-Token", secret)


def map_finnhub_news(payload: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map the news list (headline, summary, source, unix ``datetime``, ``url``); newest ``_NEWS_ITEM_CAP`` kept."""
    if isinstance(payload, dict) and payload.get("error"):
        raise ProviderPayloadError("finnhub_error_payload", _error_kind("", payload.get("error")))
    if not isinstance(payload, list):
        raise ProviderPayloadError("finnhub_response_invalid")
    records = []
    for item in payload:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        title = plain_text(item.get("headline") if isinstance(item.get("headline"), str) else None, 500)
        if not title:
            continue
        try:
            when: datetime | None = provider_instant(item.get("datetime"), collected_at, unit="s")
        except ProviderPayloadError:
            when = None
        records.append(_news_record(
            "finnhub_news", plain_text(str(item.get("source") or ""), 200) or None, f"finnhub:{item['id']}", title,
            plain_text(item.get("summary") if isinstance(item.get("summary"), str) else None, 4000),
            https_link(item.get("url")), when, collected_at,
        ))
    if not records:
        raise ProviderPayloadError("finnhub_no_items", "incomplete")
    records.sort(key=lambda r: r.observed_at, reverse=True)
    return records[:_NEWS_ITEM_CAP]


# --------------------------------------------------------------------------- NewsAPI top headlines

NEWSAPI_URL = "https://newsapi.org/v2/top-headlines?country=us&category=business&pageSize=20"


def build_newsapi(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """US business top headlines; the key rides the ``X-Api-Key`` header."""
    return place_secret("newsapi", NEWSAPI_URL, "X-Api-Key", secret)


def map_newsapi(payload: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map ``articles`` (identity = https url; title/description only, never the truncated ``content``).

    ``status: error`` bodies are classified by their ``code`` (``apiKeyInvalid`` -> credential,
    ``rateLimited`` -> back off). ``[Removed]`` placeholders are skipped.
    """
    if not isinstance(payload, dict):
        raise ProviderPayloadError("newsapi_response_invalid")
    if payload.get("status") != "ok":
        raise ProviderPayloadError("newsapi_error_payload", _error_kind(payload.get("code"), payload.get("message")))
    articles = payload.get("articles")
    if not isinstance(articles, list) or len(articles) > 100:
        raise ProviderPayloadError("newsapi_response_invalid")
    records = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        link = https_link(article.get("url"))
        title = plain_text(article.get("title") if isinstance(article.get("title"), str) else None, 500)
        if link is None or not title or title == "[Removed]":
            continue
        source = article.get("source")
        publisher = plain_text(str(source.get("name") or ""), 200) if isinstance(source, dict) else ""
        published = parse_when(article.get("publishedAt") if isinstance(article.get("publishedAt"), str) else None, collected_at)
        records.append(_news_record(
            "newsapi", publisher or None, link, title,
            plain_text(article.get("description") if isinstance(article.get("description"), str) else None, 4000),
            link, published, collected_at,
        ))
    if not records:
        raise ProviderPayloadError("newsapi_no_items", "incomplete")
    return records[:MAX_FEED_ITEMS]


# --------------------------------------------------------------------------- OpenWeather current conditions

OWM_URL = "https://api.openweathermap.org/data/2.5/weather?units=metric&lat={lat}&lon={lon}"
_OWM_METRICS = (("temperature", "main", "temp", "degC"), ("humidity", "main", "humidity", "%"),
                ("pressure", "main", "pressure", "hPa"), ("wind_speed", "wind", "speed", "m/s"))


def build_openweathermap(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Current conditions at the owner's coordinates; the key rides the ``appid`` query parameter."""
    lat = _scope_number(cfg, "owm_latitude", -90, 90, "openweathermap")
    lon = _scope_number(cfg, "owm_longitude", -180, 180, "openweathermap")
    return place_secret("openweathermap", OWM_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"), "query:appid", secret)


def map_openweathermap(payload: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map temperature, humidity, pressure and wind speed to measurements at the response instant (``dt``).

    A non-200 ``cod`` is classified (401 -> credential, 429 -> back off). The place name is display text only.
    """
    lat = _scope_number(cfg, "owm_latitude", -90, 90, "openweathermap")
    lon = _scope_number(cfg, "owm_longitude", -180, 180, "openweathermap")
    if not isinstance(payload, dict):
        raise ProviderPayloadError("openweathermap_response_invalid")
    if str(payload.get("cod")) != "200":
        raise ProviderPayloadError("openweathermap_error_payload", _error_kind(payload.get("cod"), payload.get("message")))
    observed = provider_instant(payload.get("dt"), collected_at, unit="s")
    stamp = int(observed.timestamp())
    place = plain_text(str(payload.get("name") or ""), 80) or None
    records = []
    for metric, group, field, unit in _OWM_METRICS:
        block = payload.get(group)
        if not isinstance(block, dict) or block.get(field) is None:
            continue
        try:
            value = finite_decimal(block[field])
        except ValueError as exc:
            raise ProviderPayloadError("openweathermap_value_invalid") from exc
        text = decimal_text(value)
        records.append(build_record(
            provider="openweathermap", identity=f"openweathermap:{lat:.2f},{lon:.2f}:{metric}:{stamp}",
            content=f"{place or 'Location'} {metric}: {text} {unit}", title=f"{place or 'Weather'} {metric}",
            observed_at=observed, collected_at=collected_at, version=content_version("openweathermap", lat, lon, metric, stamp, text),
            metric=metric, value=value, unit=unit, region=place, latitude=lat, longitude=lon,
            quality="provider_reported", provider_fields={"dt": stamp, "place": place},
        ))
    if not records:
        raise ProviderPayloadError("openweathermap_no_measurements", "incomplete")
    return records


# --------------------------------------------------------------------------- OpenAQ latest sensor values

OPENAQ_URL = "https://api.openaq.org/v3/locations/{location}/sensors"


def build_openaq(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Sensors (with their latest value) of the owner's location; the key rides the ``X-API-Key`` header."""
    location = cfg.get("openaq_location_id")
    if type(location) is not int or not 1 <= location <= 2_147_483_647:
        raise ProviderPayloadError("openaq_scope_invalid")
    return place_secret("openaq", OPENAQ_URL.format(location=location), "X-API-Key", secret)


def map_openaq(payload: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map each sensor's ``latest`` (value, UTC datetime, coordinates) with its parameter name and units."""
    location = cfg.get("openaq_location_id")
    if type(location) is not int:
        raise ProviderPayloadError("openaq_scope_invalid")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) > 100:
        raise ProviderPayloadError("openaq_response_invalid")
    records = []
    for sensor in results:
        latest = sensor.get("latest") if isinstance(sensor, dict) else None
        parameter = sensor.get("parameter") if isinstance(sensor, dict) else None
        if not isinstance(latest, dict) or not isinstance(parameter, dict) or type(sensor.get("id")) is not int:
            continue
        datetime_block = latest.get("datetime")
        stamp = datetime_block.get("utc") if isinstance(datetime_block, dict) else None
        name = parameter.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", name):
            continue
        try:
            observed = parse_when(stamp if isinstance(stamp, str) else None, collected_at)
            value = finite_decimal(latest.get("value"))
        except ValueError:
            continue
        if observed is None:
            continue
        coords = latest.get("coordinates")
        lat = lon = None
        if isinstance(coords, dict):
            try:
                lat_d, lon_d = finite_decimal(coords.get("latitude")), finite_decimal(coords.get("longitude"))
                if -90 <= lat_d <= 90 and -180 <= lon_d <= 180:
                    lat, lon = float(lat_d), float(lon_d)
            except ValueError:
                pass
        units = parameter.get("units")
        unit = units[:64] if isinstance(units, str) and units else "unit"
        iso, text = observed.isoformat(), decimal_text(value)
        records.append(build_record(
            provider="openaq", identity=f"openaq:{location}:{sensor['id']}:{iso}",
            content=f"OpenAQ location {location} {name}: {text} {unit} at {iso}", title=f"OpenAQ {name}",
            observed_at=observed, collected_at=collected_at, version=content_version("openaq", location, sensor["id"], iso, text),
            metric=name, value=value, unit=unit, region=f"location {location}", latitude=lat, longitude=lon,
            quality="provider_reported",
            provider_fields={"location_id": location, "sensor_id": sensor["id"], "datetime": iso},
        ))
    if not records:
        raise ProviderPayloadError("openaq_no_measurements", "incomplete")
    return records


# --------------------------------------------------------------------------- NASA FIRMS active fires (CSV)

FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/country/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{country}/1"
_COUNTRY_RE = re.compile(r"^[A-Z]{3}$")


def build_nasa_firms(secret: str, cfg: Mapping[str, Any]) -> ProviderRequest:
    """Last-24-hour VIIRS hotspots for the owner's ISO3 country; the key replaces ``{MAP_KEY}`` in the path."""
    country = _scope_text(cfg, "firms_country", _COUNTRY_RE, "nasa_firms")
    request = place_secret("nasa_firms", FIRMS_URL.replace("{country}", country), "path:MAP_KEY", secret)
    return SecretRequest(request.method, request.url, {**request.headers, "Accept": "text/csv"})


def map_nasa_firms(body: object, collected_at: datetime, cfg: Mapping[str, Any]) -> list[IngestionRecord]:
    """Map hotspots (strongest ``frp`` first, capped) to measurements with typed latitude/longitude.

    FIRMS answers a bad key with a plain-text body (no CSV header): that is a credential failure, not an
    empty success. More rows than the cap yields ``truncated`` coverage.
    """
    country = _scope_text(cfg, "firms_country", _COUNTRY_RE, "nasa_firms")
    if not isinstance(body, (bytes, bytearray)) or len(body) > KEYED_MAX_BYTES:
        raise ProviderPayloadError("provider_response_too_large")
    try:
        text = bytes(body).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderPayloadError("nasa_firms_response_invalid") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"latitude", "longitude", "acq_date", "acq_time"} <= set(reader.fieldnames):
        raise ProviderPayloadError("nasa_firms_error_payload", _error_kind("", text[:120]))
    hotspots = []
    for row in reader:
        try:
            lat_d, lon_d = finite_decimal(row["latitude"]), finite_decimal(row["longitude"])
            day = parse_iso_date(row["acq_date"])
            hhmm = row["acq_time"].zfill(4)
            frp = finite_decimal(row["frp"]) if row.get("frp") else None
            observed = datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=UTC)
        except (KeyError, ValueError, ProviderPayloadError):
            continue
        if -90 <= lat_d <= 90 and -180 <= lon_d <= 180 and (observed - collected_at).total_seconds() <= 600:
            hotspots.append((frp if frp is not None else finite_decimal(0), observed, float(lat_d), float(lon_d), row))
    if not hotspots:
        raise ProviderPayloadError("nasa_firms_no_hotspots", "incomplete")
    hotspots.sort(key=lambda h: (h[0], h[1]), reverse=True)
    coverage: Literal["returned_snapshot", "truncated"] = "truncated" if len(hotspots) > _FIRMS_CAP else "returned_snapshot"
    records = []
    for frp, observed, lat, lon, row in hotspots[:_FIRMS_CAP]:
        iso = observed.isoformat()
        confidence = (row.get("confidence") or "")[:8] or None
        satellite = (row.get("satellite") or "")[:16] or None
        records.append(build_record(
            provider="nasa_firms", identity=f"nasa_firms:{country}:{lat:.4f}:{lon:.4f}:{iso}",
            content=f"Active fire detection in {country} at {lat:.4f}, {lon:.4f} on {iso}: FRP {decimal_text(frp)} MW",
            title=f"Active fire {country}", observed_at=observed, collected_at=collected_at,
            version=content_version("nasa_firms", country, lat, lon, iso, decimal_text(frp)),
            metric="fire_radiative_power", value=frp, unit="MW", region=country, latitude=lat, longitude=lon,
            quality="provider_reported", coverage=coverage,
            provider_fields={
                "acq": iso, "confidence": confidence, "satellite": satellite,
                "daynight": (row.get("daynight") or "")[:2] or None,
            },
        ))
    return records


# --------------------------------------------------------------------------- registry

KEYED: dict[str, KeyedProvider] = {
    "coingecko": KeyedProvider(
        COINGECKO_KEY_HEADER, lambda secret, cfg: coingecko_request(secret),
        lambda payload, at, cfg: map_coingecko(payload, at)),
    "fred": KeyedProvider("query:api_key", build_fred, map_fred),
    "finnhub_news": KeyedProvider("X-Finnhub-Token", build_finnhub, map_finnhub_news),
    "newsapi": KeyedProvider("X-Api-Key", build_newsapi, map_newsapi),
    "openweathermap": KeyedProvider("query:appid", build_openweathermap, map_openweathermap),
    "openaq": KeyedProvider("X-API-Key", build_openaq, map_openaq),
    "nasa_firms": KeyedProvider("path:MAP_KEY", build_nasa_firms, map_nasa_firms, binary=True),
}
