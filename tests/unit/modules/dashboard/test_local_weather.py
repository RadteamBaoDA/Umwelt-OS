"""Local weather lookup: rounding, cache, Nominatim degradation, upstream failure, route validation."""

from typing import Any

import httpx
import pytest
from fastapi import HTTPException, Response

from modules.dashboard import local_weather as lw
from modules.dashboard import routes

CURRENT = {"current": {
    "temperature_2m": 21.4, "weather_code": 3, "relative_humidity_2m": 55, "wind_speed_10m": 6.1,
    "precipitation_probability": 5, "is_day": 1,
}, "hourly": {
    "time": [1_793_000_000 + 3600 * i for i in range(30)],
    "temperature_2m": [20 + i for i in range(30)],
    "precipitation_probability": [10, None],
    "wind_speed_10m": "bad",
}}


@pytest.fixture(autouse=True)
def _clear() -> None:
    """Isolate the per-process cache between tests."""
    lw._CACHE.clear()


def _patch(monkeypatch: pytest.MonkeyPatch, *, place_fails: bool = False, weather_fails: bool = False) -> list[dict[str, Any]]:
    """Replace the HTTP fetch, recording each call's params."""
    calls: list[dict[str, Any]] = []

    async def fake(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append({"url": url, **params})
        if url == lw._FORECAST_URL:
            if weather_fails:
                raise lw.LocalWeatherUnavailable("x")
            return CURRENT
        if place_fails:
            raise httpx.ConnectError("boom")
        return {"address": {"town": "Hội An", "state": "Quảng Nam"}}

    monkeypatch.setattr(lw, "_get_json", fake)
    return calls


async def test_rounds_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coordinates are rounded to 2 decimals before outbound calls; a repeat hits the cache."""
    calls = _patch(monkeypatch)
    first = await lw.local_weather(15.87654, 108.33219, "vi")
    again = await lw.local_weather(15.8751, 108.3321, "vi")
    assert first is again and len(calls) == 2
    assert calls[0]["latitude"] == 15.88 and calls[0]["longitude"] == 108.33
    assert first.place == "Hội An, Quảng Nam" and first.temperature_c == 21.4
    assert (first.humidity_pct, first.wind_kmh, first.precipitation_probability_pct, first.is_day) == (55, 6.1, 5, True)
    assert len(first.hourly) == 24 and calls[0]["forecast_hours"] == 24
    assert first.hourly[1].temperature_c == 21 and first.hourly[0].precipitation_probability_pct == 10
    assert first.hourly[1].precipitation_probability_pct is None and first.hourly[0].wind_kmh is None


async def test_place_failure_keeps_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nominatim failure yields place=None while weather is still returned."""
    _patch(monkeypatch, place_fails=True)
    result = await lw.local_weather(1.0, 2.0, "en")
    assert result.place is None and result.weather_code == 3


async def test_missing_extras_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only temperature and code are mandatory; absent extras come back as None."""
    _patch(monkeypatch)
    monkeypatch.setattr(lw, "_get_json", _minimal_fetch)
    result = await lw.local_weather(3.0, 4.0, "en")
    assert result.humidity_pct is None and result.wind_kmh is None and result.is_day is None
    assert result.hourly == []
    assert result.place == "Hà Nội"


async def _minimal_fetch(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Weather with only mandatory fields; a city whose region repeats its own name."""
    if url == lw._FORECAST_URL:
        return {"current": {"temperature_2m": 20, "weather_code": 0}}
    return {"address": {"city": "Hà Nội", "state": "Hà Nội"}}


async def test_weather_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open-Meteo failure raises the stable unavailable error and is not cached."""
    _patch(monkeypatch, weather_fails=True)
    with pytest.raises(lw.LocalWeatherUnavailable):
        await lw.local_weather(1.0, 2.0, "en")
    assert not lw._CACHE


async def test_route_maps_failure_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream failure becomes HTTP 502 with a stable error code."""
    _patch(monkeypatch, weather_fails=True)
    with pytest.raises(HTTPException) as exc:
        await routes.local_weather_route(object(), Response(), lat=0, lon=0, locale="en")  # type: ignore[arg-type]
    assert exc.value.status_code == 502 and exc.value.detail["code"] == "local_weather_unavailable"


def test_route_bounds_declared() -> None:
    """Out-of-range coordinates and unknown locales are rejected with 422 before any lookup."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    router = APIRouter()
    router.add_api_route("/w", routes.local_weather_route)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[routes.require_workspace_observer_read] = lambda: object()
    client = TestClient(app)
    for params in ({"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": 0, "lon": 0, "locale": "fr"}):
        assert client.get("/w", params=params).status_code == 422
