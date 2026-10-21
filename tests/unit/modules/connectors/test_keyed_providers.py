"""Keyed free-tier presets: placement, redaction, fail-closed-before-send and mappers (fakes only; no DB/network).

Response bodies are hand-built from each provider's public API documentation (no key was available to probe).
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
BODIES = {
    "fred": {"observations": [{"date": "2026-10-20", "value": "4.33"}, {"date": "2026-10-19", "value": "."}]},
    "finnhub_news": [{"id": 7, "headline": "Markets rally", "summary": "Stocks up.", "source": "Reuters",
                      "datetime": 1792576210, "url": "https://example.org/a"}],
    "newsapi": {"status": "ok", "articles": [{"source": {"name": "AP"}, "title": "Rates hold", "description": "Fed.",
                                              "url": "https://example.org/b", "publishedAt": "2026-10-21T08:00:00Z"}]},
    "openweathermap": {"cod": 200, "name": "Hanoi", "dt": 1792576210, "main": {"temp": 28.4, "humidity": 70, "pressure": 1010},
                       "wind": {"speed": 3.2}},
    "openaq": {"results": [{"id": 5, "parameter": {"name": "pm25", "units": "ug/m3"},
                            "latest": {"datetime": {"utc": "2026-10-21T08:00:00Z"}, "value": 18.5,
                                       "coordinates": {"latitude": 21.0, "longitude": 105.8}}}]},
    "nasa_firms": (b"latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,"
                   b"bright_ti5,frp,daynight\n21.1,105.9,330.1,0.4,0.4,2026-10-21,0612,N,VIIRS,n,2.0NRT,290.2,4.2,D\n"
                   b"21.2,105.7,340.0,0.4,0.4,2026-10-21,0612,N,VIIRS,h,2.0NRT,291.0,9.9,D\n"),
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
])
def test_provider_error_bodies_are_classified_not_ingested(provider, body, kind):
    with pytest.raises(ProviderPayloadError) as err:
        keyed.KEYED[provider].mapper(body, NOW, CFG[provider])
    assert err.value.kind == kind


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
