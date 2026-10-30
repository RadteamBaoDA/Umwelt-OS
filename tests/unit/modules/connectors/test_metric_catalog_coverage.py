"""Catalog-vs-adapter coverage: every metric/symbol an adapter emits must be selectable via the catalog."""

import json
from datetime import UTC, datetime, timedelta
from functools import cache
from itertools import pairwise

import pytest

from modules.connectors.providers import keyed
from modules.connectors.providers.crypto import DEFAULT_BINANCE_PAIRS
from modules.connectors.providers.world_data import (
    PURE_ADAPTERS,
    _market_records,
    map_pure_provider_body,
)
from modules.connectors.public import ConnectorConfig, provider_metrics
from tests.unit.modules.connectors.p3_helpers import fixture, jfixture, wd
from tests.unit.modules.connectors.test_keyed_providers import BODIES, CFG

# Late enough that the time-bounded fixtures (NOAA Kp, ECB) are inside their freshness windows.
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
_PURE_FIXTURES = {
    "world_bank": "world_bank_vn_gdp.json", "frankfurter": "frankfurter_usd_vnd.json",
    "ecb": "ecb_eurofxref_daily.xml", "binance": "binance_btcusdt.json", "alternative_me": "fear_greed.json",
    "usgs": "usgs_4_5_day.geojson", "coinpaprika": "coinpaprika_btc.json", "coingecko": "coingecko_btc.json",
    "noaa_kp": "noaa_kp.json", "open_er_api": "open_er_api.json", "oecd_cli": "oecd_cli.json",
    "iss_position": "iss_position.json", "disease_sh": "disease_sh.json",
}


def _open_meteo_records(collected_at: datetime) -> list:
    """Three hourly open_meteo samples (offline body shaped like the adapter's request) mapped at ``collected_at``."""
    from types import SimpleNamespace
    from uuid import UUID

    from modules.connectors.providers.world_data import _weather_records

    config = ConnectorConfig(weather_latitude=21.0, weather_longitude=105.8, weather_timezone="UTC", weather_metrics=("temperature_2m",))
    payload = {
        "timezone": "UTC", "hourly_units": {"temperature_2m": "C"},
        "hourly": {"time": ["2026-10-21T22:00", "2026-10-21T23:00", "2026-10-22T00:00"], "temperature_2m": [24.0, 23.5, 23.0]},
    }
    source = SimpleNamespace(id=UUID(int=7))
    return _weather_records(source, config, payload, collected_at, coverage="returned_snapshot")  # type: ignore[arg-type]


def _emit(provider: str, collected_at: datetime) -> list:
    """Map one provider's offline fixture as if collected at ``collected_at`` (the only input that differs between samples)."""
    if provider in _PURE_FIXTURES:
        return map_pure_provider_body(provider, fixture(_PURE_FIXTURES[provider]), collected_at)
    if provider == "alpha_vantage":
        config = ConnectorConfig(market_symbols=("IBM",), market_currency="USD", market_exchange_timezone="America/New_York")
        return _market_records(None, config, jfixture("alpha_vantage_ibm_daily.json"), collected_at)
    if provider == "open_meteo":
        return _open_meteo_records(collected_at)
    return keyed.KEYED[provider].mapper(BODIES[provider], collected_at, CFG[provider])


@cache
def _emitted() -> dict[str, list]:
    """Map provider -> records produced from the existing offline fixtures (no network)."""
    providers = [*_PURE_FIXTURES, "alpha_vantage", "open_meteo"]
    providers += [p for p in CFG if p in BODIES and p in provider_metrics_keys()]
    return {provider: _emit(provider, NOW) for provider in providers}


def provider_metrics_keys() -> set[str]:
    """Keyed providers that carry a measurement catalog entry."""
    return {p for p in CFG if provider_metrics(p) is not None}


def test_pure_fixture_table_covers_registered_adapters_with_catalog() -> None:
    """Every pure adapter that has a catalog entry is exercised here (new adapters must be added)."""
    assert {p for p in PURE_ADAPTERS if provider_metrics(p)} <= set(_PURE_FIXTURES)


@pytest.mark.parametrize("provider", sorted(_emitted()))
def test_emitted_metrics_and_symbols_match_catalog(provider: str) -> None:
    """Emitted metric ids are in the catalog (unless open) and symbol-keyed providers always set a symbol."""
    entry = provider_metrics(provider)
    assert entry is not None, provider
    records = _emitted()[provider]
    assert records
    for record in records:
        data = wd(record)
        if data["value"] is None and data["metric"] is None:
            continue
        if not entry.metrics_open:
            assert data["metric"] in {m.id for m in entry.metrics}, (provider, data["metric"])
        if entry.symbol_keyed:
            assert data["symbol"], (provider, "symbol missing")


def test_suggested_symbols_are_what_adapters_emit() -> None:
    """Suggested symbols for symbol-keyed providers appear in the emitted symbols."""
    emitted = _emitted()
    assert {pair[0] for pair in DEFAULT_BINANCE_PAIRS} <= set(provider_metrics("binance").suggested_symbols)
    for provider in ("binance", "coinpaprika", "coingecko", "frankfurter", "ecb", "open_er_api", "oecd_cli", "fred"):
        entry = provider_metrics(provider)
        symbols = {wd(r)["symbol"] for r in emitted[provider]}
        assert symbols & set(entry.suggested_symbols), (provider, symbols, entry.suggested_symbols)


def test_fx_suggestions_are_emitted_pairs() -> None:
    """Every suggested symbol of a fixture-backed provider is one the adapter emits (no invented pairs)."""
    for provider, records in _emitted().items():
        entry = provider_metrics(provider)
        if entry is None or not entry.symbol_keyed or not entry.suggested_symbols or provider in {"binance", "coingecko", "fred"}:
            continue
        assert set(entry.suggested_symbols) <= {wd(r)["symbol"] for r in records}, provider
    from modules.connectors.providers.macro import OPEN_ER_QUOTES

    assert set(provider_metrics("open_er_api").suggested_symbols) == {f"USD/{q}" for q in OPEN_ER_QUOTES}


def test_region_and_series_flags_match_adapter_output() -> None:
    """Flags are relations over what each adapter fixture emits, not a hard-coded provider list."""
    for provider, records in _emitted().items():
        entry = provider_metrics(provider)
        rows = [wd(r) for r in records if wd(r)["metric"] is not None]
        regions = [row["region"] for row in rows]
        distinct = {region for region in regions if region}
        if not entry.charts_as_series:
            # Point events: rows carry their own place/country (a place-less event has none) and no symbol,
            # so there is no shared series key.
            assert distinct, (provider, regions)
            assert all(not row["symbol"] for row in rows), provider
        if entry.region_keyed:
            assert all(regions), (provider, regions)  # a region-keyed series needs a region on every row
        elif entry.charts_as_series and not entry.symbol_keyed:
            assert len(distinct) <= 1, (provider, distinct)  # one series per source unless keyed by region
        if entry.suggested_regions:
            assert entry.region_keyed, provider
            assert set(entry.suggested_regions) <= distinct, (provider, distinct)


def test_owner_configured_symbol_providers_are_not_seeded() -> None:
    """Providers whose symbols the owner configures (fred series, binance pairs, coingecko assets) opt out of preset seeding."""
    for provider in ("fred", "binance", "coingecko"):
        assert provider_metrics(provider).seed_symbol is False
    for provider in ("coinpaprika", "frankfurter", "ecb", "open_er_api", "oecd_cli"):
        assert provider_metrics(provider).seed_symbol is True


def _series_key(record) -> tuple:
    """Series a record belongs to: what a chart plots as one line."""
    data = wd(record)
    return data["metric"], data["symbol"], data["region"]


_CHARTED = sorted(p for p in (*_PURE_FIXTURES, "alpha_vantage", "open_meteo", "fred", "openweathermap", "openaq")
                  if provider_metrics(p).charts_as_series)


@pytest.mark.parametrize("provider", _CHARTED)
def test_series_history_accumulates(provider: str) -> None:
    """Mapping the same fixture at two collection times still yields chartable history.

    History comes from observation revisions (``history=true`` window), so a fixed identity is fine as long as each new
    collection carries a distinct ``observed_at``. Either the ids are disjoint (a new row per sample), or an id is
    reused with a different ``observed_at`` (collection clock: a new revision), or the provider clock supplies the
    instant: then distinct samples of one series in a body must still have distinct ids so they cannot overwrite each other.
    """
    first = [r for r in _emit(provider, NOW) if wd(r)["metric"] is not None]
    later = [r for r in _emit(provider, NOW + timedelta(hours=3)) if wd(r)["metric"] is not None]
    assert first and len(first) == len(later), provider
    first_ids, later_ids = {r.provider_id for r in first}, {r.provider_id for r in later}
    if first_ids.isdisjoint(later_ids):
        return  # a new row per collection
    assert first_ids == later_ids, (provider, "identity must be all-or-nothing clock dependent")
    before = {r.provider_id: r.observed_at for r in first}
    if any(r.observed_at != before[r.provider_id] for r in later):
        assert all(r.observed_at != before[r.provider_id] for r in later), (provider, "collection clock must advance every revision")
        return  # same id, new observed_at: the revision history is the series
    by_series: dict[tuple, list] = {}
    for record in first:
        by_series.setdefault(_series_key(record), []).append(record)
    for key, rows in by_series.items():
        if len({r.observed_at for r in rows}) > 1:
            assert len({r.provider_id for r in rows}) == len(rows), (provider, key, "distinct samples share an id")


def test_oecd_cli_requests_history_and_maps_each_period() -> None:
    """The OECD request asks for 24 periods and a multi-period message yields distinct ids per area and period."""
    import copy

    from modules.connectors.providers.macro import OECD_CLI_URL, map_oecd_cli

    assert "lastNObservations=24" in OECD_CLI_URL
    payload = jfixture("oecd_cli.json")
    multi = copy.deepcopy(payload)
    dims = multi["data"]["structures"][0]["dimensions"]["observation"]
    period = dims[-1]
    period["values"].append({**period["values"][0], "id": "2026-08", "name": "2026-08"})
    obs = multi["data"]["dataSets"][0]["observations"]
    for key, value in list(obs.items()):
        obs[key[:-1] + "1"] = [value[0] - 1, 0, 0, 0]
    ids = [r.provider_id for r in map_oecd_cli(multi, NOW)]
    assert len(ids) == len(set(ids)) == 10
    assert {i.rsplit(":", 1)[1] for i in ids} == {"2026-09", "2026-08"}


def test_cadence_matches_fixture_spacing() -> None:
    """Fixtures with several samples per series are spaced no wider than the catalog cadence_days."""
    for provider, records in _emitted().items():
        entry = provider_metrics(provider)
        if not entry.charts_as_series or entry.cadence_days is None:
            continue
        keyed: dict[tuple, list] = {}
        for r in records:
            d = wd(r)
            if d["metric"] is not None:
                keyed.setdefault((d["metric"], d["symbol"], d["region"]), []).append(r.observed_at)
        for series in keyed.values():
            ordered = sorted(series)
            gaps = [(b - a).total_seconds() / 86400 for a, b in pairwise(ordered)]
            # 4x tolerance: weekends/holidays leave gaps in daily market and FX series.
            assert all(g <= entry.cadence_days * 4 for g in gaps), (provider, gaps, entry.cadence_days)


def _second_sample(provider: str) -> list:
    """The sample one catalog cadence after the fixture's, as the provider would deliver it next time.

    Collection-clock adapters (binance, coingecko without ``last_updated_at``) take their instant from the collection
    time; disease_sh takes the provider's ``updated`` stamp from the body, so that field is advanced.
    """
    entry = provider_metrics(provider)
    step = timedelta(days=entry.cadence_days)
    if provider == "disease_sh":
        body = jfixture(_PURE_FIXTURES[provider])
        body["updated"] += int(step.total_seconds() * 1000)
        return map_pure_provider_body(provider, json.dumps(body).encode(), NOW + step)
    return _emit(provider, NOW + step)


@pytest.mark.parametrize("provider", ["binance", "coingecko", "disease_sh"])
def test_single_sample_fixture_providers_space_samples_by_cadence(provider: str) -> None:
    """Two mapped samples of a one-sample-per-fixture provider land one catalog cadence apart on the same series."""
    entry = provider_metrics(provider)
    first = {_series_key(r): r for r in _emit(provider, NOW) if wd(r)["metric"] is not None}
    second = {_series_key(r): r for r in _second_sample(provider) if wd(r)["metric"] is not None}
    assert first and first.keys() == second.keys(), provider
    for key, record in first.items():
        gap = (second[key].observed_at - record.observed_at).total_seconds() / 86400
        assert 0 < gap <= entry.cadence_days * 4, (provider, key, gap, entry.cadence_days)
