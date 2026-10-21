"""Pure NASA EONET v3 natural-event adapter (open events, fixed endpoint, no key). Event/source URLs are never fetched."""

import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    decimal_text,
    finite_decimal,
    naive_utc_instant,
    provider_instant,
)
from modules.ingestion.schemas import IngestionRecord

NASA_EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=50"
_MAX_EVENTS = 50  # equals the request limit; more is reported incomplete rather than truncated silently
_MAX_SOURCES = 5
_LOG = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def nasa_eonet_request() -> ProviderRequest:
    """Return the single fixed, keyless EONET open-events request."""
    return ProviderRequest("GET", NASA_EONET_URL)


def _clean(value: object, limit: int) -> str | None:
    """Return printable text trimmed to ``limit`` characters, or None for non-text/empty input."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit] or None


def _truncated(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return metadata with the provider envelope coverage downgraded to ``truncated``."""
    out = dict(metadata)
    out["provider_record"] = {**out["provider_record"], "coverage": "truncated"}
    return out


def _map_event(
    event_id: str, title: str, event: dict[str, object], collected_at: datetime,
) -> IngestionRecord:
    """Map one event with a valid id/title to a record; malformed geometry/magnitude raises ``ProviderPayloadError``."""
    geometry = event.get("geometry")
    if not isinstance(geometry, list) or not geometry or not all(isinstance(g, dict) for g in geometry):
        raise ProviderPayloadError("nasa_eonet_geometry_invalid")
    dated = [(provider_instant(g.get("date"), collected_at, unit="iso"), g) for g in geometry]
    observed, latest = max(dated, key=lambda pair: pair[0])
    lat = lon = None
    if latest.get("type") == "Point":
        coords = latest.get("coordinates")
        try:
            if not isinstance(coords, list) or len(coords) != 2:
                raise ValueError("coordinates")
            lon_d, lat_d = (finite_decimal(c) for c in coords)
        except ValueError as exc:
            raise ProviderPayloadError("nasa_eonet_coordinates_invalid") from exc
        if not (-180 <= lon_d <= 180 and -90 <= lat_d <= 90):
            raise ProviderPayloadError("nasa_eonet_coordinates_invalid")
        lat, lon = float(lat_d), float(lon_d)
    value: Decimal | None = None
    unit = _clean(latest.get("magnitudeUnit"), 32)
    if latest.get("magnitudeValue") is not None:
        try:
            value = finite_decimal(latest["magnitudeValue"])
        except ValueError as exc:
            raise ProviderPayloadError("nasa_eonet_magnitude_invalid") from exc
    categories_raw = event.get("categories")
    categories = [
        c for c in (_clean(cat.get("title"), 60) for cat in categories_raw if isinstance(cat, dict)) if c
    ] if isinstance(categories_raw, list) else []
    sources_raw = event.get("sources")
    links = [
        u for u in (_clean(s.get("url"), 300) for s in sources_raw if isinstance(s, dict))
        if u and u.startswith(("http://", "https://"))
    ][:_MAX_SOURCES] if isinstance(sources_raw, list) else []
    closed = _clean(event.get("closed"), 40)
    category_text = ", ".join(categories)
    content = f"{title} [{category_text or 'uncategorized'}]" + (f" Sources: {' '.join(links)}" if links else "")
    event_ms = int(observed.timestamp() * 1000)
    fields: dict[str, str | float | int | None] = {
        "categories": category_text or None, "closed": closed, "latitude": lat, "longitude": lon,
        "event_time": event_ms, "magnitude_unit": unit,
    }
    return build_record(
        provider="nasa_eonet", identity=event_id, content=content, title=title[:500],
        observed_at=observed, collected_at=collected_at,
        version=content_version("nasa_eonet", event_id, event_ms, closed, value),
        metric="natural_event", value=value, unit=unit or "event", symbol=None,
        region=_clean(event.get("description"), 80), latitude=lat, longitude=lon,
        quality="provider_reported", timestamp_basis="provider_modified", modified_at=observed,
        provider_fields=fields,
    )


def map_nasa_eonet(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map each open event to one record keyed by event id; observed_at is the latest geometry date.

    Raises ProviderPayloadError for malformed shapes, duplicate ids, bad times/coordinates or more
    events than the requested limit. Individually malformed events are skipped and counted; a full page
    (limit reached) or any skip yields ``truncated`` coverage. Only http(s) source links are kept, as text, never fetched.
    """
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ProviderPayloadError("nasa_eonet_response_invalid")
    if len(events) > _MAX_EVENTS:
        raise ProviderPayloadError("nasa_eonet_event_limit_exceeded", "incomplete")
    seen: set[str] = set()
    valid: list[tuple[str, str, dict[str, object]]] = []
    skipped = 0
    for event in events:
        event_id = event.get("id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not _ID_RE.fullmatch(event_id):
            skipped += 1  # no stable identity: one bad event must not fail the batch
            continue
        if event_id in seen:
            raise ProviderPayloadError("nasa_eonet_event_invalid")  # duplicate ids break snapshot integrity
        seen.add(event_id)
        title = _clean(event.get("title"), 300)
        if not title:
            skipped += 1
            continue
        valid.append((event_id, title, event))
    records = []
    for event_id, title, event in valid:
        try:
            records.append(_map_event(event_id, title, event, collected_at))
        except ProviderPayloadError:
            skipped += 1
    if events and not records:
        raise ProviderPayloadError("nasa_eonet_event_invalid")
    if skipped:
        _LOG.warning("nasa_eonet skipped %d malformed event(s) of %d", skipped, len(events))
    # EONET v3 has no pagination for this query: a full page may hide more events and skipped events are
    # missing, so either way the batch is not a complete snapshot.
    if skipped or len(events) >= _MAX_EVENTS:
        records = [r.model_copy(update={"metadata": _truncated(r.metadata)}) for r in records]
    return records


NOAA_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
_KP_MAX_ROWS = 24  # newest 24 three-hour bins = the last three days


def noaa_kp_request() -> ProviderRequest:
    """Return the fixed keyless NOAA SWPC planetary K-index request."""
    return ProviderRequest("GET", NOAA_KP_URL)


def map_noaa_kp(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map the newest ``_KP_MAX_ROWS`` three-hour planetary Kp bins to one measurement each.

    ``time_tag`` is the bin start in UTC (sent without a zone). Kp is a 0-9 index; anything else, a
    non-list body or a malformed row raises ``ProviderPayloadError`` instead of yielding a partial series.
    """
    if not isinstance(payload, list) or not payload or not all(isinstance(r, dict) for r in payload):
        raise ProviderPayloadError("noaa_kp_response_invalid")
    rows = []
    for row in payload:
        observed = naive_utc_instant(row.get("time_tag"), collected_at)
        try:
            kp = finite_decimal(row.get("Kp"))
        except ValueError as exc:
            raise ProviderPayloadError("noaa_kp_value_invalid") from exc
        if not 0 <= kp <= 9:
            raise ProviderPayloadError("noaa_kp_value_invalid")
        rows.append((observed, kp, row))
    rows.sort(key=lambda item: item[0])
    records = []
    for observed, kp, row in rows[-_KP_MAX_ROWS:]:
        tag, text = observed.isoformat(), decimal_text(kp)
        running, stations = row.get("a_running"), row.get("station_count")
        records.append(build_record(
            provider="noaa_kp", identity=f"noaa_kp:{tag}", content=f"Planetary Kp index {tag}: {text}",
            title="NOAA planetary Kp index", observed_at=observed, collected_at=collected_at,
            version=content_version("noaa_kp", tag, text), metric="kp_index", value=kp, unit="kp",
            quality="provider_reported",
            provider_fields={
                "time_tag": tag, "a_running": running if type(running) is int else None,
                "station_count": stations if type(stations) is int else None,
            },
        ))
    return records


HAZARD_ADAPTERS: dict[str, PureAdapter] = {
    "nasa_eonet": PureAdapter(nasa_eonet_request, map_nasa_eonet),
    "noaa_kp": PureAdapter(noaa_kp_request, map_noaa_kp),
}
