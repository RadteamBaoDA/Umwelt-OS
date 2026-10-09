"""Pure USGS earthquake adapter (M4.5+ past-day GeoJSON feed). Detail URLs are never fetched or stored."""

import re
from datetime import datetime
from decimal import Decimal

from modules.connectors.providers.macro import (
    ProviderPayloadError,
    ProviderRequest,
    PureAdapter,
    build_record,
    content_version,
    decimal_text,
    finite_decimal,
    provider_instant,
)
from modules.ingestion.schemas import IngestionRecord

USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
_MAX_FEATURES = 500  # ReceiveBatch record limit; more is reported incomplete rather than truncated silently
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_ .-]{1,32}$")


def usgs_request() -> ProviderRequest:
    return ProviderRequest("GET", USGS_URL)


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit] or None


def map_usgs(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map each event: feature.id identity, event time observed, ``updated`` modification and version."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ProviderPayloadError("usgs_response_invalid")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ProviderPayloadError("usgs_response_invalid")
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        if "count" in meta and meta["count"] != len(features):
            raise ProviderPayloadError("usgs_count_mismatch", "incomplete")
        if "status" in meta and meta["status"] != 200:
            raise ProviderPayloadError("usgs_error_payload")
    if len(features) > _MAX_FEATURES:
        raise ProviderPayloadError("usgs_feature_limit_exceeded", "incomplete")
    seen: set[str] = set()
    records = []
    for feature in features:
        if not isinstance(feature, dict):
            raise ProviderPayloadError("usgs_feature_invalid")
        event_id, props, geometry = feature.get("id"), feature.get("properties"), feature.get("geometry")
        if not isinstance(event_id, str) or not _ID_RE.fullmatch(event_id) or event_id in seen:
            raise ProviderPayloadError("usgs_feature_id_invalid")
        seen.add(event_id)
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(props, dict) or not isinstance(coords, list) or len(coords) != 3:
            raise ProviderPayloadError("usgs_feature_invalid")
        try:
            lon, lat, depth = (finite_decimal(c) for c in coords)
        except ValueError as exc:
            raise ProviderPayloadError("usgs_coordinates_invalid") from exc
        if not (-180 <= lon <= 180 and -90 <= lat <= 90 and -50 <= depth <= 1000):
            raise ProviderPayloadError("usgs_coordinates_invalid")
        occurred = provider_instant(props.get("time"), collected_at, unit="ms")
        updated = provider_instant(props.get("updated"), collected_at, unit="ms")
        if updated < occurred:
            raise ProviderPayloadError("usgs_time_invalid")
        raw_mag = props.get("mag")
        try:
            mag: Decimal | None = None if raw_mag is None else finite_decimal(raw_mag)
        except ValueError as exc:
            raise ProviderPayloadError("usgs_magnitude_invalid") from exc
        if mag is not None and not -2 <= mag <= 10:
            raise ProviderPayloadError("usgs_magnitude_invalid")
        place = _clean(props.get("place"), 200)
        mag_type = props.get("magType")
        mag_text = "null" if mag is None else decimal_text(mag)
        updated_ms = int(updated.timestamp() * 1000)
        occurred_ms = int(occurred.timestamp() * 1000)
        fields: dict[str, str | float | int | None] = {
            "place": place, "type": _clean(props.get("type"), 32), "status": _clean(props.get("status"), 32),
            "magType": mag_type if isinstance(mag_type, str) and _TOKEN_RE.fullmatch(mag_type) else None,
            "depth_km": float(depth), "latitude": float(lat), "longitude": float(lon),
            "event_time": occurred_ms, "updated": updated_ms,
        }
        records.append(build_record(
            provider="usgs", identity=f"usgs:{event_id}",
            content=f"Earthquake M{'?' if mag is None else mag_text} {place or 'unknown location'} at {occurred.isoformat()}",
            title=f"M{'?' if mag is None else mag_text} {place or 'earthquake'}"[:500],
            observed_at=occurred, collected_at=collected_at,
            version=content_version("usgs", event_id, updated_ms, mag_text, fields["status"]),
            metric="earthquake_magnitude", value=mag, unit="magnitude", symbol=None,
            region=(place or "")[:80] or None, latitude=float(lat), longitude=float(lon),
            quality="provider_reported", timestamp_basis="provider_modified", modified_at=updated,
            provider_fields=fields, decimal_key="decimal_mag",
        ))
    return records


DISASTER_ADAPTERS: dict[str, PureAdapter] = {"usgs": PureAdapter(usgs_request, map_usgs)}
