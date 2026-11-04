"""Wave F per-type options: request URLs, mapper scope, client filters, validation, fan-out, quota and 422 routes.

Fakes only (no database, no network, no live provider calls).
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from modules.connectors import builtins, collection
from modules.connectors import provider_options as po
from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.providers import crypto, hazards, json_feed, macro, rest, security
from modules.connectors.providers.world_data import map_pure_provider_body
from tests.unit.modules.connectors.p3_helpers import NOW, fixture, jfixture, wd
from tests.unit.modules.connectors.test_builtins import _RAW, SCOPE, _request, _session, _World
from tests.unit.modules.connectors.test_free_provider_dispatch import Gate, make_run

LATE = datetime(2026, 10, 22, tzinfo=UTC)  # after the fixtures' provider timestamps

WAVE_F = (
    "wikipedia_mostread", "wikipedia_top_views", "satnogs_observations", "disease_sh", "world_bank", "frankfurter",
    "ecb", "open_er_api", "oecd_cli", "binance", "coinpaprika", "noaa_kp", "us_travel_advisories",
)


def url(provider: str, cfg: dict, cursor: str | None = None) -> str | None:
    """Shorthand for ``build_url`` at the fixed test instant."""
    return po.build_url(provider, cfg, cursor=cursor, now=NOW)


# ------------------------------------------------------------------ request URLs (golden defaults + options)

@pytest.mark.parametrize("provider", WAVE_F)
def test_empty_config_keeps_todays_request(provider):
    assert url(provider, {}) is None


def test_default_valued_options_also_keep_todays_request():
    assert url("wikipedia_mostread", {"wiki_language": "en"}) is None
    assert url("satnogs_observations", {"satnogs_status": "good"}) is None
    assert url("world_bank", {"wb_country": "VN", "wb_indicator": "gdp"}) is None
    assert url("frankfurter", {"fx_base": "USD", "fx_quotes": ["VND"]}) is None
    assert url("open_er_api", {"fx_base": "USD"}) is None
    assert url("oecd_cli", {"oecd_countries": ["GBR", "USA", "CHN", "JPN", "DEU"]}) is None
    assert url("binance", {"crypto_symbols": ["BTCUSDT"]}) is None


def test_wikipedia_language_in_both_templates():
    assert url("wikipedia_mostread", {"wiki_language": "vi"}) == (
        "https://api.wikimedia.org/feed/v1/wikipedia/vi/featured/2026/10/08")
    assert url("wikipedia_top_views", {"wiki_language": "ja"}) == (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/ja.wikipedia/all-access/2026/10/07")


def test_satnogs_status():
    assert url("satnogs_observations", {"satnogs_status": "bad"}) == (
        "https://network.satnogs.org/api/observations/?format=json&status=bad")


def test_disease_country_and_world_bank():
    assert url("disease_sh", {"disease_country": "VN"}) == "https://disease.sh/v3/covid-19/countries/VN"
    assert url("world_bank", {"wb_country": "US"}) == (
        "https://api.worldbank.org/v2/country/US/indicator/NY.GDP.MKTP.CD?format=json&per_page=3")
    assert url("world_bank", {"wb_indicator": "inflation_cpi"}) == (
        "https://api.worldbank.org/v2/country/VN/indicator/FP.CPI.TOTL.ZG?format=json&per_page=3")


def test_fx_urls():
    assert url("frankfurter", {"fx_base": "EUR"}) == "https://api.frankfurter.dev/v2/rate/eur/vnd"
    assert url("frankfurter", {"fx_quotes": ["JPY"]}) == "https://api.frankfurter.dev/v2/rate/usd/jpy"
    assert url("frankfurter", {"fx_base": "EUR", "fx_quotes": ["VND", "USD"]}) == (
        "https://api.frankfurter.dev/v2/rates?base=EUR&quotes=USD,VND")
    assert url("open_er_api", {"fx_base": "EUR"}) == "https://open.er-api.com/v6/latest/EUR"
    assert url("open_er_api", {"fx_quotes": ["VND"]}) is None  # quote subset is a client filter, request unchanged
    assert url("ecb", {"fx_quotes": ["USD"]}) is None


def test_oecd_and_binance_urls():
    assert url("oecd_cli", {"oecd_countries": ["FRA", "USA"]}) == (
        "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA+FRA.M.LI...AA...H"
        "?lastNObservations=24&dimensionAtObservation=AllDimensions")
    assert url("binance", {"crypto_symbols": ["ETHUSDT"]}) == (
        "https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDT")
    assert url("binance", {"crypto_symbols": ["ETHUSDT", "BTCUSDT"]}) == (
        "https://data-api.binance.vision/api/v3/ticker/price?symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D")


def test_coinpaprika_one_url_per_coin_default_and_selected():
    assert po.coin_urls({}) == ["https://api.coinpaprika.com/v1/tickers/btc-bitcoin"]
    assert po.coin_urls({"coin_ids": ["eth-ethereum", "btc-bitcoin"]}) == [
        "https://api.coinpaprika.com/v1/tickers/btc-bitcoin", "https://api.coinpaprika.com/v1/tickers/eth-ethereum"]


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("provider,cfg", [
    ("frankfurter", {"fx_base": "USD", "fx_quotes": ["USD"]}),
    ("frankfurter", {"fx_quotes": ["VND", "VND"]}),
    ("ecb", {"fx_quotes": ["VND"]}),  # ECB publishes no VND
    ("open_er_api", {"fx_base": "EUR", "fx_quotes": ["EUR"]}),
    ("oecd_cli", {"oecd_countries": ["USA", "USA"]}),
    ("coinpaprika", {"coin_ids": ["btc-bitcoin", "btc-bitcoin"]}),
    ("binance", {"crypto_symbols": ["BTCUSDT", "BTCUSDT"]}),
])
def test_validate_options_rejects_bad_combinations(provider, cfg):
    with pytest.raises(ValueError):
        po.validate_options(provider, cfg)


def test_connector_config_rejects_out_of_enum_and_too_many_values():
    from modules.connectors.public import ConnectorConfig

    for bad in (
        {"wiki_language": "xx"}, {"satnogs_status": "ugly"}, {"wb_indicator": "bitcoin"}, {"fx_base": "ZZZ"},
        {"crypto_symbols": ["FOOUSDT"]}, {"coin_ids": ["a-b"] * 2}, {"kp_min": 10}, {"advisory_min_level": 5},
        {"oecd_countries": ["USA"] * 11}, {"fx_quotes": ["USD"] * 11}, {"disease_country": "Vietnam"},
        {"coin_ids": ["btc-bitcoin", "eth-ethereum", "bnb-binance-coin", "sol-solana", "xrp-xrp", "ada-cardano"]},
    ):
        with pytest.raises(ValidationError):
            ConnectorConfig(**bad)


# ------------------------------------------------------------------ mapper scope

def test_world_bank_non_gdp_indicator_has_its_own_unit():
    payload = [{"page": 1, "pages": 1, "per_page": 3, "total": 1}, [{
        "indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation"}, "country": {"id": "US", "value": "United States"},
        "countryiso3code": "USA", "date": "2024", "value": 2.9}]]
    kw = po.map_options("world_bank", {"wb_country": "US", "wb_indicator": "inflation_cpi"})
    (record,) = macro.map_world_bank(payload, NOW, **kw)
    assert wd(record)["metric"] == "inflation_cpi_pct" and wd(record)["unit"] == "annual %"
    assert wd(record).get("currency") is None


def test_frankfurter_default_unchanged_and_multi_quote_list():
    (default,) = macro.map_frankfurter(jfixture("frankfurter_usd_vnd.json"), NOW)
    assert default.provider_id == "frankfurter:USD:VND:2026-10-07:blended" or default.provider_id.startswith("frankfurter:USD:VND:")
    rows = [{"date": "2026-10-07", "base": "EUR", "quote": q, "rate": r} for q, r in (("USD", 1.1), ("VND", 28000))]
    kw = po.map_options("frankfurter", {"fx_base": "EUR", "fx_quotes": ["USD", "VND"]})
    records = macro.map_frankfurter(rows, NOW, **kw)
    assert [r.provider_id for r in records] == [
        "frankfurter:EUR:USD:2026-10-07:blended", "frankfurter:EUR:VND:2026-10-07:blended"]
    with pytest.raises(macro.ProviderPayloadError):  # a quote we did not ask for is a scope mismatch
        macro.map_frankfurter(rows[:1] + [{**rows[1], "quote": "JPY"}], NOW, **kw)


def test_open_er_api_other_base():
    payload = jfixture("open_er_api.json")
    payload["base_code"] = "EUR"
    payload["rates"]["USD"] = 1.1
    kw = po.map_options("open_er_api", {"fx_base": "EUR"})
    records = macro.map_open_er_api(payload, LATE, **kw)
    assert {r.provider_id.split(":")[2] for r in records} == {"VND", "JPY", "GBP", "CNY", "USD"}
    assert all(r.provider_id.startswith("open_er_api:EUR:") for r in records)


def test_disease_sh_country_scope():
    payload = {**jfixture("disease_sh.json"), "country": "Vietnam", "countryInfo": {"iso2": "VN"}}
    records = security.map_disease_sh(payload, LATE, country="VN")
    assert records[0].provider_id == "disease_sh:VN:cases" and wd(records[0])["region"] == "Vietnam"
    with pytest.raises(macro.ProviderPayloadError):
        security.map_disease_sh(payload, LATE, country="US")
    assert security.map_disease_sh(jfixture("disease_sh.json"), LATE)[0].provider_id == "disease_sh:global:cases"


def test_coinpaprika_other_coin():
    payload = jfixture("coinpaprika_btc.json")
    payload.update(id="eth-ethereum", symbol="ETH")
    (record,) = crypto.map_coinpaprika(payload, NOW, coin_id="eth-ethereum")
    assert record.provider_id.startswith("coinpaprika:eth-ethereum:USD:") and wd(record)["symbol"] == "ETH"
    with pytest.raises(macro.ProviderPayloadError):
        crypto.map_coinpaprika(payload, NOW)  # default still demands BTC


def test_binance_batch_maps_through_pairs():
    payload = [{"symbol": "BTCUSDT", "price": "100"}, {"symbol": "ETHUSDT", "price": "3000"}]
    kw = po.map_options("binance", {"crypto_symbols": ["ETHUSDT", "BTCUSDT"]})
    assert [r.provider_id for r in crypto.map_binance(payload, NOW, **kw)] == [
        "binance:BTCUSDT:price", "binance:ETHUSDT:price"]


def test_oecd_ten_countries_stay_under_observation_bound():
    assert macro._SDMX_MAX_OBSERVATIONS >= 10 * 24


def test_wikipedia_language_feed_has_language_link_and_main_page_skip():
    body = json.dumps({"items": [{"articles": [
        {"article": "Trang_Chính", "views": 9, "rank": 1}, {"article": "Hà_Nội", "views": 5, "rank": 2}]}]}).encode()
    feed = json_feed.JSON_FEEDS["wikipedia_top_views"]
    kw = po.map_options("wikipedia_top_views", {"wiki_language": "vi"})
    records = map_pure_provider_body("wikipedia_top_views", body, NOW, **kw)
    assert len(records) == 1 and "vi.wikipedia.org/wiki/H" in json.dumps(records[0].metadata)
    assert feed.link_template.startswith("https://en.wikipedia.org")  # default row untouched
    assert len(map_pure_provider_body("wikipedia_top_views", body, NOW)) == 2  # default language keeps both


# ------------------------------------------------------------------ client filters

def _ecb():
    return macro.map_ecb(fixture("ecb_eurofxref_daily.xml"), NOW)


def test_ecb_and_open_er_quote_subset():
    assert len(po.filter_records("ecb", {}, _ecb())) == len(_ecb()) > 2
    kept = po.filter_records("ecb", {"fx_quotes": ["JPY", "USD"]}, _ecb())
    assert sorted(r.provider_id.split(":")[2] for r in kept) == ["JPY", "USD"]
    oer = macro.map_open_er_api(jfixture("open_er_api.json"), LATE)
    assert [r.provider_id.split(":")[2] for r in po.filter_records("open_er_api", {"fx_quotes": ["VND"]}, oer)] == ["VND"]


def test_noaa_kp_min_threshold():
    records = hazards.map_noaa_kp(jfixture("noaa_kp.json"), LATE)
    values = sorted(float(wd(r)["value"]) for r in records)
    assert values[0] < values[-1]
    kept = po.filter_records("noaa_kp", {"kp_min": values[-1]}, records)
    assert kept and all(float(wd(r)["value"]) >= values[-1] for r in kept)
    assert len(po.filter_records("noaa_kp", {"kp_min": 0}, records)) == len(records)


def test_us_travel_min_level_parsed_from_title():
    from modules.ingestion.schemas import IngestionRecord

    def rec(title):
        return IngestionRecord(provider_id=title, content=title, observed_at=NOW, collected_at=NOW,
                               metadata={"title": title, "provider_record": {"coverage": "returned_snapshot"}})
    rows = [rec("Japan - Level 1: Exercise Normal Precautions"), rec("Mexico - Level 3: Reconsider Travel"),
            rec("Haiti - Level 4: Do Not Travel"), rec("Worldwide Caution")]
    kept = po.filter_records("us_travel_advisories", {"advisory_min_level": 3}, rows)
    assert [r.provider_id for r in kept] == [rows[1].provider_id, rows[2].provider_id, rows[3].provider_id]
    assert len(po.filter_records("us_travel_advisories", {}, rows)) == 4


def test_satnogs_fingerprint_changes_with_status_only():
    base = po.native_fingerprint("satnogs_observations", {})
    assert base == po.native_fingerprint("satnogs_observations", {"satnogs_status": "good", "max_items": 3})
    assert base != po.native_fingerprint("satnogs_observations", {"satnogs_status": "bad"})


# ------------------------------------------------------------------ runners

async def test_pure_run_uses_option_url_and_scoped_mapper(monkeypatch):
    payload = [{"symbol": "BTCUSDT", "price": "100"}, {"symbol": "ETHUSDT", "price": "3000"}]
    gate = Gate({"*": rest.Fetched(json.dumps(payload).encode())})
    run, accepted = make_run("binance", gate, monkeypatch, configuration={"crypto_symbols": ["BTCUSDT", "ETHUSDT"]})
    await collection._run_pure(run)
    assert len(gate.calls) == 1 and "symbols=" in gate.calls[0][0]  # ONE batched call
    assert len(accepted[0][0]) == 2


async def test_pure_run_filters_ecb_subset(monkeypatch):
    gate = Gate({"*": rest.Fetched(fixture("ecb_eurofxref_daily.xml"))})
    run, accepted = make_run("ecb", gate, monkeypatch, configuration={"fx_quotes": ["USD"]})
    await collection._run_pure(run)
    assert [r.provider_id.split(":")[2] for r in accepted[0][0]] == ["USD"]


async def test_coinpaprika_one_gated_call_per_coin_with_pacing(monkeypatch):
    def ticker(coin, symbol):
        payload = jfixture("coinpaprika_btc.json")
        payload.update(id=coin, symbol=symbol)
        return rest.Fetched(json.dumps(payload).encode())
    base = "https://api.coinpaprika.com/v1/tickers/"
    gate = Gate({base + "btc-bitcoin": ticker("btc-bitcoin", "BTC"), base + "eth-ethereum": ticker("eth-ethereum", "ETH")})
    run, accepted = make_run("coinpaprika", gate, monkeypatch, configuration={"coin_ids": ["btc-bitcoin", "eth-ethereum"]})
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(collection.asyncio, "sleep", fake_sleep)
    await collection._run_coinpaprika(run)
    assert [c[0] for c in gate.calls] == [base + "btc-bitcoin", base + "eth-ethereum"]  # each call debits quota
    assert sleeps == [po.COINPAPRIKA_GAP_SECONDS] and po.COINPAPRIKA_GAP_SECONDS >= 1.0  # pilot cap is 1 call/second
    assert len(accepted[0][0]) == 2


def test_coinpaprika_and_hn_quota_cover_max_fan_out_at_fastest_schedule():
    for provider, calls_per_run in (("coinpaprika", 5), ("hn_top", 31)):
        spec = get_provider_spec(provider)
        daily = next(p.limit_units for p in spec.quota if p.window == "day" and p.limit_units)
        assert daily >= calls_per_run * 96, provider  # 96 runs/day at the fastest 15-minute schedule
    minute = next(p.limit_units for p in get_provider_spec("hn_top").quota if p.window == "minute")
    assert minute >= 31  # one run (listing + 30 items) must fit one minute window or it defers forever


async def test_social_walk_reports_caught_up_in_run_stats(monkeypatch):
    seen = {}

    class _Session:
        async def __aenter__(self):
            return SimpleNamespace(rollback=lambda: None)

        async def __aexit__(self, *a):
            return False

    async def fake_accept(session, batch, **kw):
        seen["stats"] = kw["state_update"].stats if kw.get("state_update") else None

    monkeypatch.setattr(collection.ingestion, "accept_native_collection", fake_accept)
    source = SimpleNamespace(id=uuid4(), generation=1, provider="bluesky", configuration={})
    attempt = SimpleNamespace(source=source, connector_revision=1, scope=SimpleNamespace(), multi=False, ref=None)
    run = SimpleNamespace(attempt=attempt, gate=SimpleNamespace(captured_operation=None), extras={},
                          factory=lambda: _Session())

    async def no_publish(*_a, **_k):
        return None
    monkeypatch.setattr(collection, "_publish_realtime_snapshot", no_publish)
    lease = SimpleNamespace(token=uuid4(), cursor_before=None)
    await collection._accept_native(run, lease, (), "truncated", datetime.now(UTC))
    assert seen["stats"]["caught_up"] is False
    run.extras.clear()
    await collection._accept_native(run, lease, (), "returned_snapshot", datetime.now(UTC))
    assert seen["stats"]["caught_up"] is True


# ------------------------------------------------------------------ enable route: 422s (waves D and F)

@pytest.mark.parametrize("bad", [
    {"hn_list": "hot"}, {"gdelt_timespan": "2h"}, {"usgs_period": "year"}, {"min_magnitude": 9.5}, {"eonet_days": 31},
    {"nvd_window_days": 8}, {"nws_area": "ZZ"}, {"ioda_country": "vn"}, {"region": "atlantis"},
    {"wiki_language": "xx"}, {"satnogs_status": "ugly"}, {"wb_indicator": "bitcoin"}, {"fx_base": "ZZZ"},
    {"crypto_symbols": ["FOOUSDT"]}, {"coin_ids": ["nope"] * 6}, {"kp_min": 10}, {"advisory_min_level": 0},
    {"oecd_countries": ["USA"] * 11}, {"disease_country": "Vietnam"},
])
def test_enable_request_rejects_invalid_option_values(bad):
    with pytest.raises(ValidationError):  # FastAPI turns a body ValidationError into HTTP 422
        builtins.BuiltinEnableRequest(**bad)


@pytest.mark.parametrize("provider,option", [
    ("tuoitre_news", {"hn_list": "show"}), ("binance", {"usgs_period": "week"}), ("usgs", {"wiki_language": "vi"}),
    ("hn_top", {"crypto_symbols": ["BTCUSDT"]}), ("ecb", {"fx_base": "EUR"}), ("open_er_api", {"fx_quotes": ["BGN"]}),
    ("noaa_kp", {"advisory_min_level": 2}), ("us_travel_advisories", {"kp_min": 3}),
    ("world_bank", {"fx_quotes": ["USD"]}), ("coinpaprika", {"crypto_symbols": ["BTCUSDT"]}),
    ("emsc_quakes", {"nvd_severity": ["HIGH"]}), ("alternative_me", {"coin_ids": ["btc-bitcoin"]}),
    ("iss_position", {"kp_min": 1}),
])
async def test_enable_wrong_provider_or_unsupported_value_is_422(provider, option):
    with _World(provider), pytest.raises(HTTPException) as exc:
        await builtins.enable_builtin(provider, _session(), _request(), SCOPE, _RAW(**option))  # type: ignore[arg-type]
    assert exc.value.status_code == 422


@pytest.mark.parametrize("provider,option", [
    ("wikipedia_top_views", {"wiki_language": "vi"}), ("satnogs_observations", {"satnogs_status": "bad"}),
    ("disease_sh", {"disease_country": "VN"}), ("world_bank", {"wb_country": "US", "wb_indicator": "gdp_growth"}),
    ("frankfurter", {"fx_base": "EUR", "fx_quotes": ["USD", "JPY"]}), ("ecb", {"fx_quotes": ["USD"]}),
    ("open_er_api", {"fx_base": "EUR", "fx_quotes": ["USD"]}), ("oecd_cli", {"oecd_countries": ["USA", "FRA"]}),
    ("binance", {"crypto_symbols": ["BTCUSDT", "ETHUSDT"]}), ("coinpaprika", {"coin_ids": ["btc-bitcoin", "eth-ethereum"]}),
    ("noaa_kp", {"kp_min": 5}), ("us_travel_advisories", {"advisory_min_level": 3}),
])
async def test_enable_saves_valid_wave_f_options_and_lists_them_as_applicable(provider, option):
    with _World(provider) as world:
        await builtins.enable_builtin(provider, _session(), _request(), SCOPE, _RAW(**option))  # type: ignore[arg-type]
    saved = world.row.desired_configuration
    assert all(saved[k] == (list(v) if isinstance(v, (list, tuple)) else v) for k, v in option.items())
    from modules.connectors.provider_specs import applicable_options
    assert set(option) <= set(applicable_options(provider))


async def test_measurement_snapshots_get_no_selection_options():
    from modules.connectors.provider_specs import applicable_options
    for provider in ("binance", "coinpaprika", "frankfurter", "ecb", "open_er_api", "noaa_kp", "iss_position", "alternative_me"):
        assert not {"max_items", "include_keywords", "exclude_keywords", "max_age_days"} & set(applicable_options(provider))
