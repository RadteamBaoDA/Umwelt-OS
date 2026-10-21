"""Pure ISS position adapter (wheretheiss.at, keyless, fixed endpoint). One point per fetch; no tracking history."""

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

ISS_URL = "https://api.wheretheiss.at/v1/satellites/25544"
_VISIBILITY = {"daylight", "visible", "eclipsed"}


def iss_position_request() -> ProviderRequest:
    """Return the fixed keyless ISS (NORAD 25544) position request."""
    return ProviderRequest("GET", ISS_URL)


def map_iss_position(payload: object, collected_at: datetime) -> list[IngestionRecord]:
    """Map one ISS position; ``value`` is altitude in km with typed latitude/longitude.

    Raises ``ProviderPayloadError`` for a wrong satellite id, non-kilometre units, out-of-range
    coordinates or a bad timestamp. Each fetch is a distinct point (identity includes the provider timestamp).
    """
    if not isinstance(payload, dict) or payload.get("id") != 25544:
        raise ProviderPayloadError("iss_position_response_invalid")
    if payload.get("units") != "kilometers":
        raise ProviderPayloadError("iss_position_units_invalid")
    observed = provider_instant(payload.get("timestamp"), collected_at, unit="s")
    try:
        lat, lon = finite_decimal(payload.get("latitude")), finite_decimal(payload.get("longitude"))
        altitude, velocity = finite_decimal(payload.get("altitude")), finite_decimal(payload.get("velocity"))
    except ValueError as exc:
        raise ProviderPayloadError("iss_position_value_invalid") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and altitude > 0 and velocity >= 0):
        raise ProviderPayloadError("iss_position_value_invalid")
    visibility = payload.get("visibility")
    stamp = int(observed.timestamp())
    lat_f, lon_f = round(float(lat), 4), round(float(lon), 4)
    alt_km = altitude.quantize(Decimal("0.1"))
    return [build_record(
        provider="iss_position", identity=f"iss_position:25544:{stamp}",
        content=f"ISS at {lat_f}, {lon_f}, altitude {alt_km} km",
        title="ISS position", observed_at=observed, collected_at=collected_at,
        version=content_version("iss_position", stamp, lat_f, lon_f), metric="iss_altitude", value=altitude,
        unit="km", latitude=lat_f, longitude=lon_f, quality="provider_reported",
        provider_fields={
            "velocity_kmh": float(velocity), "timestamp": stamp,
            "visibility": visibility if visibility in _VISIBILITY else None,
        },
    )]


SPACE_ADAPTERS: dict[str, PureAdapter] = {"iss_position": PureAdapter(iss_position_request, map_iss_position)}
