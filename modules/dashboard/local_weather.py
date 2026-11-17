"""Browser-geolocation weather lookup for the Today header.

Coordinates arrive per request, are rounded to ~1 km before any outbound call, and are
never persisted or logged. Only fixed HTTPS hosts (Open-Meteo, Nominatim) are contacted.
"""

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "Umwelt-OS/1.0 (self-hosted)"
_TTL_SECONDS = 600
_HOURS = 24
_MAX_ENTRIES = 64
_MAX_BYTES = 64 * 1024
# ponytail: per-process cache; each API worker keeps its own copy. Move to Redis if workers multiply.
_CACHE: dict[tuple[float, float, str], tuple[float, "LocalWeather"]] = {}


class LocalWeatherUnavailable(Exception):
    """Raised when the weather provider cannot supply a usable current reading."""


class HourlyPoint(BaseModel):
    """One forecast hour; readings the provider omitted are None."""

    at: datetime
    temperature_c: float | None = None
    precipitation_probability_pct: float | None = None
    wind_kmh: float | None = None


class LocalWeather(BaseModel):
    """Current conditions for a rounded position, with optional place name.

    Secondary readings (humidity, wind, rain chance, day flag) are None when Open-Meteo omits them.
    """

    temperature_c: float
    weather_code: int
    place: str | None
    humidity_pct: float | None = None
    wind_kmh: float | None = None
    precipitation_probability_pct: float | None = None
    is_day: bool | None = None
    observed_at: datetime
    hourly: list[HourlyPoint] = []
    attribution: list[str] = ["Open-Meteo", "OpenStreetMap"]


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET one fixed-host JSON document without redirects, rejecting oversized or non-object bodies."""
    response = await client.get(
        url, params=params, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
    )
    if response.is_redirect or response.status_code >= 400 or len(response.content) > _MAX_BYTES:
        raise LocalWeatherUnavailable("local_weather_unavailable")
    decoded = response.json()
    if not isinstance(decoded, dict):
        raise LocalWeatherUnavailable("local_weather_unavailable")
    return decoded


async def _place(client: httpx.AsyncClient, lat: float, lon: float, locale: str) -> str | None:
    """Reverse-geocode a place name; any failure yields None so weather is still returned."""
    try:
        data = await _get_json(
            client,
            _REVERSE_URL,
            {
                "format": "jsonv2",
                "lat": lat,
                "lon": lon,
                "zoom": 10,
                "accept-language": locale,
            },
        )
    except (httpx.HTTPError, ValueError, LocalWeatherUnavailable):
        return None
    address = data.get("address")
    if not isinstance(address, dict):
        return None
    names = [address.get(key) for key in ("city", "town", "village", "county", "state")]
    names = [str(name) for name in names if isinstance(name, str) and name]
    if not names:
        return None
    # "Locality, Region" like a weather card; a city that is itself the region (e.g. Hanoi) shows once.
    region = (
        str(address["state"])
        if isinstance(address.get("state"), str) and address["state"]
        else None
    )
    return names[0] if region is None or region == names[0] else f"{names[0]}, {region}"


async def _fetch(lat: float, lon: float, locale: str) -> LocalWeather:
    """Call Open-Meteo and Nominatim concurrently; only the weather call is mandatory."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8), trust_env=False, follow_redirects=False, verify=True
    ) as client:
        weather_task = _get_json(
            client,
            _FORECAST_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m,precipitation_probability,is_day",
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
                "forecast_hours": _HOURS,
                "timeformat": "unixtime",
                "timezone": "auto",
            },
        )
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
    return LocalWeather(
        temperature_c=float(temp),
        weather_code=code,
        place=place,
        observed_at=datetime.now(UTC),
        humidity_pct=_number(current.get("relative_humidity_2m")),
        wind_kmh=_number(current.get("wind_speed_10m")),
        precipitation_probability_pct=_number(current.get("precipitation_probability")),
        is_day=bool(current["is_day"]) if isinstance(current.get("is_day"), int) else None,
        hourly=_hourly(data.get("hourly")),
    )


def _hourly(block: object) -> list[HourlyPoint]:
    """Zip Open-Meteo's parallel hourly arrays (unix-second times) into at most `_HOURS` points.

    A malformed block yields an empty list; the current reading is still returned.
    """
    if not isinstance(block, dict) or not isinstance(block.get("time"), list):
        return []

    def column(name: str) -> list[object]:
        """Return the named hourly column from the provider block, or an empty list when it is missing or not a list."""
        values = block.get(name)
        return values if isinstance(values, list) else []

    temps, rain, wind = (
        column("temperature_2m"),
        column("precipitation_probability"),
        column("wind_speed_10m"),
    )
    points: list[HourlyPoint] = []
    for index, stamp in enumerate(block["time"][:_HOURS]):
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            continue
        points.append(
            HourlyPoint(
                at=datetime.fromtimestamp(stamp, UTC),
                temperature_c=_number(temps[index]) if index < len(temps) else None,
                precipitation_probability_pct=_number(rain[index]) if index < len(rain) else None,
                wind_kmh=_number(wind[index]) if index < len(wind) else None,
            )
        )
    return points


def _number(value: object) -> float | None:
    """Return a finite numeric reading as float; anything else (missing, null, text) is None."""
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


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
