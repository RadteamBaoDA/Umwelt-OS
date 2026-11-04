"""Browser-geolocation weather lookup for the Today header.

Coordinates arrive per request, are rounded to ~1 km before any outbound call, and are
never persisted or logged. Only fixed HTTPS hosts (Open-Meteo, Nominatim) are contacted.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "Umwelt-OS/1.0 (self-hosted)"
_TTL_SECONDS = 600
_MAX_ENTRIES = 64
_MAX_BYTES = 64 * 1024
# ponytail: per-process cache; each API worker keeps its own copy. Move to Redis if workers multiply.
_CACHE: dict[tuple[float, float, str], tuple[float, "LocalWeather"]] = {}


class LocalWeatherUnavailable(Exception):
    """Raised when the weather provider cannot supply a usable current reading."""


class LocalWeather(BaseModel):
    """Current temperature and condition code for a rounded position, with optional place name."""

    temperature_c: float
    weather_code: int
    place: str | None
    observed_at: datetime
    attribution: list[str] = ["Open-Meteo", "OpenStreetMap"]


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET one fixed-host JSON document without redirects, rejecting oversized or non-object bodies."""
    response = await client.get(url, params=params, headers={"Accept": "application/json", "User-Agent": _USER_AGENT})
    if response.is_redirect or response.status_code >= 400 or len(response.content) > _MAX_BYTES:
        raise LocalWeatherUnavailable("local_weather_unavailable")
    decoded = response.json()
    if not isinstance(decoded, dict):
        raise LocalWeatherUnavailable("local_weather_unavailable")
    return decoded


async def _place(client: httpx.AsyncClient, lat: float, lon: float, locale: str) -> str | None:
    """Reverse-geocode a place name; any failure yields None so weather is still returned."""
    try:
        data = await _get_json(client, _REVERSE_URL, {
            "format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "accept-language": locale,
        })
    except (httpx.HTTPError, ValueError, LocalWeatherUnavailable):
        return None
    address = data.get("address")
    if not isinstance(address, dict):
        return None
    for key in ("city", "town", "village", "county", "state"):
        if isinstance(address.get(key), str) and address[key]:
            return str(address[key])
    return None


async def _fetch(lat: float, lon: float, locale: str) -> LocalWeather:
    """Call Open-Meteo and Nominatim concurrently; only the weather call is mandatory."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(8), trust_env=False, follow_redirects=False, verify=True) as client:
        weather_task = _get_json(client, _FORECAST_URL, {
            "latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code", "timezone": "auto",
        })
        try:
            data, place = await asyncio.gather(weather_task, _place(client, lat, lon, locale))
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalWeatherUnavailable("local_weather_unavailable") from exc
    current = data.get("current")
    if not isinstance(current, dict):
        raise LocalWeatherUnavailable("local_weather_unavailable")
    temp, code = current.get("temperature_2m"), current.get("weather_code")
    if not isinstance(temp, int | float) or not isinstance(code, int):
        raise LocalWeatherUnavailable("local_weather_unavailable")
    return LocalWeather(temperature_c=float(temp), weather_code=code, place=place, observed_at=datetime.now(UTC))


async def local_weather(lat: float, lon: float, locale: str) -> LocalWeather:
    """Return cached or fresh weather for coordinates rounded to 2 decimals.

    Raises:
        LocalWeatherUnavailable: Open-Meteo failed or returned an unusable payload.
    """
    key = (round(lat, 2), round(lon, 2), locale)
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    result = await _fetch(key[0], key[1], locale)
    if len(_CACHE) >= _MAX_ENTRIES:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
    _CACHE[key] = (now, result)
    return result
