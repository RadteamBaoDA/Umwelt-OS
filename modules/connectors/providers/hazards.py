"""Pure NASA EONET v3 natural-event adapter (open events, fixed endpoint, no key). Event/source URLs are never fetched."""

import re
from datetime import datetime
from decimal import Decimal

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    finite_decimal,
    provider_instant,
)
from modules.ingestion.schemas import IngestionRecord

NASA_EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=50"
_MAX_EVENTS = 50  # equals the request limit; more is reported incomplete rather than truncated silently
_MAX_SOURCES = 5
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


def map_nasa_eonet(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map each open event to one record keyed by event id; observed_at is the latest geometry date.

    Raises ProviderPayloadError for malformed shapes, duplicate ids, bad times/coordinates or more
    events than the requested limit. Only http(s) source links are kept, as text, never fetched.
    """
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ProviderPayloadError("nasa_eonet_response_invalid")
    if len(events) > _MAX_EVENTS:
        raise ProviderPayloadError("nasa_eonet_event_limit_exceeded", "incomplete")
    seen: set[str] = set()
    records = []
    for event in events:
        if not isinstance(event, dict):
            raise ProviderPayloadError("nasa_eonet_event_invalid")
        event_id, title = event.get("id"), _clean(event.get("title"), 300)
        if not isinstance(event_id, str) or not _ID_RE.fullmatch(event_id) or event_id in seen or not title:
            raise ProviderPayloadError("nasa_eonet_event_invalid")
        seen.add(event_id)
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
        records.append(build_record(
            provider="nasa_eonet", identity=event_id, content=content, title=title[:500],
            observed_at=observed, collected_at=collected_at,
            version=content_version("nasa_eonet", event_id, event_ms, closed, value),
            metric="natural_event", value=value, unit=unit or "event", symbol=None,
            region=_clean(event.get("description"), 80), latitude=lat, longitude=lon,
            quality="provider_reported", timestamp_basis="provider_modified", modified_at=observed,
            provider_fields=fields,
        ))
    return records


HAZARD_ADAPTERS: dict[str, PureAdapter] = {"nasa_eonet": PureAdapter(nasa_eonet_request, map_nasa_eonet)}
