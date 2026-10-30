"""Keyed free-tier presets: placement, redaction, fail-closed-before-send and mappers (fakes only; no DB/network).

Response bodies replay each provider's documented response shape (no key was available to probe live).
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors import collection
from modules.connectors.providers import keyed, rest
from modules.connectors.providers.macro import ProviderPayloadError

KEY = "SECRET-key-0123456789abcdef"
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
CFG = {
    "fred": {"fred_series_id": "DFF"},
    "finnhub_news": {},
    "newsapi": {},
    "openweathermap": {"owm_latitude": 21.0285, "owm_longitude": 105.8542},
    "openaq": {"openaq_location_id": 8118},
    "nasa_firms": {"firms_country": "VNM"},
}
# Shapes follow each provider's official reference (FRED series/observations, Finnhub /news, NewsAPI top-headlines,
# OpenWeather 2.5 current weather, OpenAQ v3 locations/{id}/sensors, FIRMS country CSV), trimmed but with the full
# envelope and the extra fields real responses carry, so a mapper that only works on a minimal body is caught.
BODIES = {
    "fred": {"realtime_start": "2026-10-22", "realtime_end": "2026-10-22", "observation_start": "1776-07-04",
             "observation_end": "9999-12-31", "units": "lin", "output_type": 1, "file_type": "json",
             "order_by": "observation_date", "sort_order": "desc", "count": 2, "offset": 0, "limit": 5,
             "observations": [{"realtime_start": "2026-10-22", "realtime_end": "2026-10-22", "date": "2026-10-20", "value": "4.33"},
                              {"realtime_start": "2026-10-22", "realtime_end": "2026-10-22", "date": "2026-10-19", "value": "."}]},
    "finnhub_news": [{"category": "general", "datetime": 1792576210, "headline": "Markets rally", "id": 7,
                      "image": "https://example.org/a.jpg", "related": "", "source": "Reuters", "summary": "Stocks up.",
                      "url": "https://example.org/a"}],
    "newsapi": {"status": "ok", "totalResults": 1,
                "articles": [{"source": {"id": None, "name": "AP"}, "author": "A. Writer", "title": "Rates hold",
                              "description": "Fed.", "url": "https://example.org/b", "urlToImage": "https://example.org/b.jpg",
                              "publishedAt": "2026-10-21T08:00:00Z", "content": "Fed holds rates [+3510 chars]"}]},
    "openweathermap": {"coord": {"lon": 105.8542, "lat": 21.0285}, "weather": [{"id": 501, "main": "Rain", "description": "moderate rain", "icon": "10d"}],
                       "base": "stations",
                       "main": {"temp": 28.4, "feels_like": 31.2, "temp_min": 27.0, "temp_max": 29.0, "pressure": 1010,
                                "humidity": 70, "sea_level": 1010, "grnd_level": 1008},
                       "visibility": 10000, "wind": {"speed": 3.2, "deg": 349, "gust": 5.1}, "rain": {"1h": 3.16}, "clouds": {"all": 100},
                       "dt": 1792576210, "sys": {"type": 2, "id": 2075663, "country": "VN", "sunrise": 1792536187, "sunset": 1792582248},
                       "timezone": 25200, "id": 1581130, "name": "Hanoi", "cod": 200},
    "openaq": {"meta": {"name": "openaq-api", "website": "/", "page": 1, "limit": 100, "found": 1},
               "results": [{"id": 5, "name": "pm25 ug/m3", "parameter": {"id": 2, "name": "pm25", "units": "ug/m3", "displayName": "PM2.5"},
                            "datetimeFirst": {"utc": "2016-03-06T20:00:00Z", "local": "2016-03-07T03:00:00+07:00"},
                            "datetimeLast": {"utc": "2026-10-21T08:00:00Z", "local": "2026-10-21T15:00:00+07:00"},
                            "coverage": {"expectedCount": 1, "expectedInterval": "01:00:00", "observedCount": 58447,
                                         "percentComplete": 100.0, "percentCoverage": 100.0},
                            "latest": {"datetime": {"utc": "2026-10-21T08:00:00Z", "local": "2026-10-21T15:00:00+07:00"}, "value": 18.5,
                                       "coordinates": {"latitude": 21.0, "longitude": 105.8}},
                            "summary": {"min": 1.0, "max": 90.0, "avg": 30.1}}]},
    "nasa_firms": (b"country_id,latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,"
                   b"bright_ti5,frp,daynight\nVNM,21.1,105.9,330.1,0.4,0.4,2026-10-21,0612,N,VIIRS,n,2.0NRT,290.2,4.2,D\n"
                   b"VNM,21.2,105.7,340.0,0.4,0.4,2026-10-21,0612,N,VIIRS,h,2.0NRT,291.0,9.9,D\n"),
}


@pytest.mark.parametrize("provider", sorted(CFG))
def test_key_never_appears_in_repr_and_lands_at_its_placement(provider):
    row = keyed.KEYED[provider]
    request = row.build(KEY, CFG[provider])
    assert KEY not in repr(request)
    kind, _, name = row.placement.partition(":")
    if kind == "query":
        assert f"{name}={KEY}" in request.url and KEY not in json.dumps(request.headers)
    elif kind == "path":
        assert f"/{KEY}/" in request.url and "{" not in request.url
    else:
        assert request.headers[row.placement] == KEY and KEY not in request.url


@pytest.mark.parametrize("provider", sorted(CFG))
def test_fixture_maps_and_records_never_contain_the_key(provider):
    row = keyed.KEYED[provider]
    records = row.mapper(BODIES[provider], NOW, CFG[provider])
    assert records and all(r.metadata["provider_record"]["provider"] == provider for r in records)
    assert KEY not in json.dumps([r.model_dump(mode="json") for r in records])


def test_scope_is_required_before_any_request_is_built():
    for provider in ("fred", "openweathermap", "openaq", "nasa_firms"):
        with pytest.raises(ProviderPayloadError, match=f"{provider}_scope_invalid"):
            keyed.KEYED[provider].build(KEY, {})
    with pytest.raises(ProviderPayloadError, match="fred_scope_invalid"):
        keyed.KEYED["fred"].build(KEY, {"fred_series_id": "dff;rm"})


@pytest.mark.parametrize("secret", ["", " padded ", "x" * 300, "bad\nkey"])
def test_unusable_keys_are_refused(secret):
    with pytest.raises(ProviderPayloadError) as err:
        keyed.validate_key("fred", secret)
    assert err.value.kind == "credential"


def test_path_key_cannot_add_segments():
    request = keyed.KEYED["nasa_firms"].build("a/b?c", CFG["nasa_firms"])
    assert "a%2Fb%3Fc" in request.url


@pytest.mark.parametrize(("provider", "body", "kind"), [
    ("fred", {"error_code": 400, "error_message": "Bad Request. The value for variable api_key is not registered."}, "credential"),
    ("newsapi", {"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid."}, "credential"),
    ("newsapi", {"status": "error", "code": "rateLimited", "message": "too many requests"}, "rate_limited"),
    ("openweathermap", {"cod": 401, "message": "Invalid API key."}, "credential"),
    ("finnhub_news", {"error": "Invalid API key"}, "credential"),
    ("nasa_firms", b"Invalid MAP_KEY.", "credential"),
    ("fred", {"error_code": 429, "error_message": "Too Many Requests. Rate limit exceeded."}, "rate_limited"),
    ("newsapi", {"status": "error", "code": "apiKeyMissing", "message": "Your API key is missing. Append this to the URL with the apiKey param, or use the x-api-key HTTP header."}, "credential"),
    ("newsapi", {"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid or incorrect. Check your key, or go to https://newsapi.org to create a free API key."}, "credential"),
    ("openweathermap", {"cod": 401, "message": "Invalid API key. Please see https://openweathermap.org/faq#error401 for more info."}, "credential"),
    ("openweathermap", {"cod": 429, "message": "Your account is temporary blocked due to exceeding of requests limitation of your subscription type."}, "rate_limited"),
])
def test_provider_error_bodies_are_classified_not_ingested(provider, body, kind):
    with pytest.raises(ProviderPayloadError) as err:
        keyed.KEYED[provider].mapper(body, NOW, CFG[provider])
    assert err.value.kind == kind


def test_documented_fixtures_map_to_exact_values():
    """Replay each documented body and pin the mapped metric/value/unit/identity, not just non-emptiness."""
    (fred,) = keyed.KEYED["fred"].mapper(BODIES["fred"], NOW, CFG["fred"])
    assert (fred.metadata["provider_record"]["world_data"]["value"], fred.provider_id) == (4.33, "fred:DFF:2026-10-20")
    (news,) = keyed.KEYED["finnhub_news"].mapper(BODIES["finnhub_news"], NOW, {})
    assert (news.metadata["provider_record"]["source_fields"]["title"], news.observed_at.timestamp()) == ("Markets rally", 1792576210)
    (headline,) = keyed.KEYED["newsapi"].mapper(BODIES["newsapi"], NOW, {})
    assert headline.metadata["provider_record"]["source_fields"]["title"] == "Rates hold" and headline.observed_at == datetime(2026, 10, 21, 8, tzinfo=UTC)
    weather = {r.metadata["provider_record"]["world_data"]["metric"]: r.metadata["provider_record"]["world_data"]
               for r in keyed.KEYED["openweathermap"].mapper(BODIES["openweathermap"], NOW, CFG["openweathermap"])}
    assert {m: (w["value"], w["unit"]) for m, w in weather.items()} == {
        "temperature": (28.4, "degC"), "humidity": (70, "%"), "pressure": (1010, "hPa"), "wind_speed": (3.2, "m/s")}
    (air,) = keyed.KEYED["openaq"].mapper(BODIES["openaq"], NOW, CFG["openaq"])
    world = air.metadata["provider_record"]["world_data"]
    assert (world["metric"], world["value"], world["unit"]) == ("pm25", 18.5, "ug/m3")


def test_fred_skips_missing_marker_and_firms_orders_by_power():
    (fred,) = keyed.KEYED["fred"].mapper(BODIES["fred"], NOW, CFG["fred"])
    assert fred.provider_id == "fred:DFF:2026-10-20"
    firms = keyed.KEYED["nasa_firms"].mapper(BODIES["nasa_firms"], NOW, CFG["nasa_firms"])
    assert [r.metadata["provider_record"]["world_data"]["value"] for r in firms] == [9.9, 4.2]


# ------------------------------------------------------------------ executor: fail closed, one send, no leaks

class Gate:
    def __init__(self, body):
        self.body, self.calls, self.fingerprint = body, [], None

    def bind_credential(self, fingerprint):
        self.fingerprint = fingerprint

    async def fetch_bytes(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return rest.Fetched(self.body)


def make_run(monkeypatch, provider, secret, body=b"{}"):
    accepted, leased = [], []

    async def lease(_run):
        leased.append(1)
        return object()

    async def accept(_run, _lease, records, coverage, _at, update=None):
        accepted.append(records)

    async def stored(_run):
        if isinstance(secret, Exception):
            raise secret
        return secret

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(collection, "_rest_secret", stored)
    settings = SimpleNamespace(connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "x" * 44))
    source = SimpleNamespace(id=uuid4(), provider=provider, configuration=CFG[provider])
    run = SimpleNamespace(attempt=SimpleNamespace(source=source), gate=Gate(body), settings=settings, extras={})
    return run, accepted, leased


def test_every_keyed_provider_is_dispatchable_and_placement_is_the_stored_header():
    assert set(keyed.KEYED) == set(collection.KEYED_PROVIDERS) and set(CFG) | {"coingecko"} == set(keyed.KEYED)
    assert all(collection.ADAPTERS[p] is not None for p in keyed.KEYED)
    assert collection.KEYED_PROVIDERS["coingecko"] == "x-cg-demo-api-key"


@pytest.mark.parametrize("provider", sorted(CFG))
async def test_missing_or_misplaced_key_fails_before_lease_and_send(monkeypatch, provider):
    for secret in (rest.ProviderHttpError(401), ("x-other-header", KEY)):
        run, accepted, leased = make_run(monkeypatch, provider, secret)
        with pytest.raises(collection.CredentialMissing):
            await collection._run_keyed(run)
        assert not run.gate.calls and not leased and not accepted


@pytest.mark.parametrize("provider", sorted(CFG))
async def test_one_gated_send_and_accept_with_key_bound_by_fingerprint(monkeypatch, provider):
    body = BODIES[provider] if isinstance(BODIES[provider], bytes) else json.dumps(BODIES[provider]).encode()
    run, accepted, _ = make_run(monkeypatch, provider, (keyed.KEYED[provider].placement, KEY), body)
    await collection._run_keyed(run)
    assert len(run.gate.calls) == 1 and accepted and accepted[0]
    assert run.gate.fingerprint and KEY not in run.gate.fingerprint
    assert KEY not in repr(accepted)


def test_keyed_import_pins_http_loggers_to_warning() -> None:
    """Query/path-placed keys ride in the URL, so httpx/httpcore must not log requests at INFO."""
    import logging

    import modules.connectors.providers.keyed  # noqa: F401

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
