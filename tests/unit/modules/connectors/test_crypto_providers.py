import pytest

from modules.connectors.providers.crypto import (
    BINANCE_URL,
    COINGECKO_URL,
    coingecko_request,
    map_binance,
    map_coingecko,
    map_coinpaprika,
    map_fear_greed,
)
from modules.connectors.providers.macro import ProviderPayloadError
from modules.connectors.providers.world_data import map_pure_provider_body
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture, wd


def test_binance_usdt_collection_basis_and_version_ignores_clock():
    (record,) = map_binance(jfixture("binance_btcusdt.json"), NOW)
    data = wd(record)
    assert record.provider_id == "binance:BTCUSDT:price" and record.observed_at == NOW
    assert data["unit"] == "USDT" and data["currency"] is None and data["symbol"] == "BTCUSDT"
    assert data["provider_fields"] == {"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT", "decimal_value": "67123.45"}
    meta = record.metadata["provider_record"]
    assert meta["timestamp_basis"] == "collection" and meta["provider_modified_at"] is None
    (later,) = map_binance(jfixture("binance_btcusdt.json"), NOW.replace(hour=9))
    assert later.version == record.version and later.observed_at != record.observed_at


@pytest.mark.parametrize("payload", [
    [], "x", {}, {"symbol": "ETHUSDT", "price": "1"}, {"symbol": "BTCUSDT"}, {"symbol": "BTCUSDT", "price": "0"},
    {"symbol": "BTCUSDT", "price": "-1"}, {"symbol": "BTCUSDT", "price": None}, {"symbol": "BTCUSDT", "price": True},
    {"symbol": "BTCUSDT", "price": "NaN"}, {"code": -1121, "msg": "Invalid symbol."},
])
def test_binance_hostile(payload):
    with pytest.raises(ProviderPayloadError):
        map_binance(payload, NOW)


def test_binance_rate_limit_kind():
    with pytest.raises(ProviderPayloadError) as err:
        map_binance({"code": -1003, "msg": "Too many requests"}, NOW)
    assert err.value.kind == "rate_limited"


def test_fear_greed_points():
    records = map_fear_greed(jfixture("fear_greed.json"), NOW)
    assert [r.provider_id for r in records] == ["alternative_me:fng:1759881600", "alternative_me:fng:1759795200"]
    data = wd(records[0])
    assert data["value"] == 25 and data["unit"] == "index (0-100)" and data["currency"] is None
    assert data["provider_fields"]["value_classification"] == "Extreme Fear"
    assert records[0].metadata["provider_record"]["timestamp_basis"] == "provider_published"
    assert records[0].observed_at.isoformat() == "2025-10-08T00:00:00+00:00"


def _fng(**item):
    return {"data": [{"value": "50", "value_classification": "Neutral", "timestamp": "1759881600", **item}],
            "metadata": {"error": None}}


@pytest.mark.parametrize("payload", [
    [], {}, {"data": [], "metadata": {"error": None}},
    {"data": [{"value": "50"}], "metadata": {"error": "Limit exceeded"}},
    {"data": [{"value": "50", "value_classification": "N", "timestamp": "1"}]},
    _fng(value="101"), _fng(value="-1"), _fng(value="50.5"), _fng(value=True), _fng(value=None), _fng(value="x"),
    _fng(timestamp="abc"), _fng(timestamp="99999999999"), _fng(timestamp=None), _fng(value_classification="<b>"),
    {"data": [dict(_fng()["data"][0])] * 2, "metadata": {"error": None}},
    {"data": [dict(_fng()["data"][0]) for _ in range(3)], "metadata": {"error": None}},
])
def test_fear_greed_hostile(payload):
    with pytest.raises(ProviderPayloadError):
        map_fear_greed(payload, NOW)


def test_coinpaprika_usd_and_empty_price_missing():
    (record,) = map_coinpaprika(jfixture("coinpaprika_btc.json"), NOW)
    data = wd(record)
    assert data["unit"] == "USD" and data["currency"] == "USD" and data["symbol"] == "BTC"
    assert data["provider_fields"]["decimal_value"] == "67123.456789"
    assert record.metadata["provider_record"]["timestamp_basis"] == "provider_modified"
    assert record.observed_at.isoformat() == "2026-10-08T01:00:00+00:00"
    payload = jfixture("coinpaprika_btc.json")
    payload["quotes"]["USD"]["price"] = ""
    missing = wd(map_coinpaprika(payload, NOW)[0])
    assert missing["value"] is None and missing["quality"] == "missing" and missing["missing_reason"] == "provider_null"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(id="eth-ethereum"), lambda p: p.update(symbol="ETH"), lambda p: p.pop("last_updated"),
    lambda p: p.update(last_updated="2026-10-08T01:00:00"), lambda p: p.update(last_updated="2999-01-01T00:00:00Z"),
    lambda p: p.update(quotes={}), lambda p: p["quotes"]["USD"].update(price=0), lambda p: p["quotes"]["USD"].update(price=None),
    lambda p: p["quotes"]["USD"].update(price=False), lambda p: p.update(quotes={"USDT": {"price": 1}}),
])
def test_coinpaprika_hostile(mutate):
    payload = jfixture("coinpaprika_btc.json")
    mutate(payload)
    with pytest.raises(ProviderPayloadError):
        map_coinpaprika(payload, NOW)


@pytest.mark.parametrize(("payload", "kind"), [
    ({"error": "Too many requests"}, "rate_limited"), ({"error": "Payment required for this plan"}, "entitlement"),
    ({"error": "Something"}, "invalid"),
])
def test_coinpaprika_error_kinds(payload, kind):
    with pytest.raises(ProviderPayloadError) as err:
        map_coinpaprika(payload, NOW)
    assert err.value.kind == kind


def test_coingecko_collection_basis_and_optional_timestamp():
    (record,) = map_coingecko(jfixture("coingecko_btc.json"), NOW)
    assert record.provider_id == "coingecko:bitcoin:USD" and record.observed_at == NOW
    assert record.metadata["provider_record"]["timestamp_basis"] == "collection"
    assert wd(record)["provider_fields"] == {"coin_id": "bitcoin", "quote": "USD", "decimal_value": "67123.4"}
    (stamped,) = map_coingecko({"bitcoin": {"usd": 5, "last_updated_at": 1759881600}}, NOW)
    assert stamped.provider_id == "coingecko:bitcoin:USD:1759881600" and wd(stamped)["provider_fields"]["last_updated_at"] == 1759881600
    (null,) = map_coingecko({"bitcoin": {"usd": None}}, NOW)
    assert wd(null)["quality"] == "missing"


@pytest.mark.parametrize(("payload", "kind"), [
    ({"status": {"error_code": 429, "error_message": "x"}}, "rate_limited"),
    ({"status": {"error_code": 10002}}, "credential"), ({"status": {"error_code": 10010}}, "credential"),
    ({"status": {"error_code": 10011}}, "credential"), ({"error_code": 401, "error": "x"}, "credential"),
    ({"status": {"error_code": 10005}}, "entitlement"), ({"status": {"error_code": 500}}, "invalid"),
    ({"error": "x"}, "invalid"), ({}, "invalid"), ({"bitcoin": {}}, "invalid"), ({"bitcoin": {"eur": 1}}, "invalid"),
    ({"bitcoin": {"usd": 0}}, "invalid"), ({"bitcoin": {"usd": True}}, "invalid"), ({"ethereum": {"usd": 1}}, "invalid"),
    ({"bitcoin": {"usd": 1, "last_updated_at": "soon"}}, "invalid"),
])
def test_coingecko_hostile_kinds(payload, kind):
    with pytest.raises(ProviderPayloadError) as err:
        map_coingecko(payload, NOW)
    assert err.value.kind == kind


def test_coingecko_key_only_in_header():
    req = coingecko_request("k-123")
    assert req.headers == {"x-cg-demo-api-key": "k-123"} and "k-123" not in req.url and "k-123" not in repr(req)
    assert req.url == COINGECKO_URL
    for bad in ("", " k", "k\n", "x" * 300):
        with pytest.raises(ProviderPayloadError):
            coingecko_request(bad)


def test_body_dispatch_and_urls_match_catalog():
    from urllib.parse import urlsplit

    from modules.connectors.provider_specs import get_provider_spec
    from modules.connectors.providers.world_data import PURE_ADAPTERS

    assert map_pure_provider_body("binance", fixture("binance_btcusdt.json"), NOW)[0].provider_id == "binance:BTCUSDT:price"
    assert BINANCE_URL.startswith("https://data-api.binance.vision/")
    assert set(PURE_ADAPTERS) == {"world_bank", "frankfurter", "ecb", "binance", "alternative_me", "usgs", "coinpaprika", "coingecko"}
    for provider, adapter in PURE_ADAPTERS.items():
        spec = get_provider_spec(provider)
        request = adapter.request("key") if provider == "coingecko" else adapter.request()
        parts = urlsplit(request.url)
        assert request.method == "GET" and parts.scheme == "https" and parts.hostname in spec.hosts
        assert parts.path + ("?" + parts.query if parts.query else "") in spec.endpoints
        assert not parts.username and not parts.password
