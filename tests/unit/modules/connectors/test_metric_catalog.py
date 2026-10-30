"""Tests for the static per-provider metric catalog."""

import pytest

from modules.connectors.backends import PROVIDER_SOURCE_TYPES
from modules.connectors.provider_specs import get_provider_spec
from modules.connectors.public import metric_catalog, provider_metrics, source_data_shapes


def test_every_measurement_provider_has_catalog_entry() -> None:
    """Every provider whose shapes include measurement lists at least one metric."""
    for provider, source_type in PROVIDER_SOURCE_TYPES.items():
        if "measurement" in source_data_shapes(provider, source_type):
            entry = provider_metrics(provider)
            assert entry is not None and entry.metrics, provider


def test_pinned_entries() -> None:
    """Pinned ids and keying match the adapters."""
    assert [m.id for m in provider_metrics("alpha_vantage").metrics] == ["open", "high", "low", "close", "volume"]
    assert provider_metrics("alpha_vantage").symbol_keyed
    assert not provider_metrics("noaa_kp").symbol_keyed
    assert [m.id for m in provider_metrics("noaa_kp").metrics] == ["kp_index"]
    assert provider_metrics("bbc_world") is None and provider_metrics(None) is None


def test_ids_unique_per_provider() -> None:
    """Metric ids never repeat within one provider."""
    for provider, entry in metric_catalog().items():
        ids = [m.id for m in entry.metrics]
        assert len(ids) == len(set(ids)), provider


def test_fred_catalog_matches_adapter_emission() -> None:
    """FRED rows are written as metric ``fred_series`` keyed by series id, never per-series metric names."""
    entry = provider_metrics("fred")
    assert [m.id for m in entry.metrics] == ["fred_series"]
    assert entry.symbol_keyed and "DFF" in entry.suggested_symbols
    assert not entry.metrics_open


def test_oecd_cli_is_keyed_by_country() -> None:
    """OECD CLI emits five countries; the catalog keys them by ISO area symbol."""
    entry = provider_metrics("oecd_cli")
    assert entry.symbol_keyed and set(entry.suggested_symbols) == {"USA", "CHN", "JPN", "DEU", "GBR"}


def test_binance_suggestions_are_adapter_pairs() -> None:
    """Binance writes the trading pair (e.g. BTCUSDT) as symbol, not the base asset."""
    from modules.connectors.providers.crypto import DEFAULT_BINANCE_PAIRS

    assert set(provider_metrics("binance").suggested_symbols) == {pair[0] for pair in DEFAULT_BINANCE_PAIRS}


def test_default_lookback_covers_two_observation_periods() -> None:
    """Every charted series window (own default, else the generic 90 days) spans at least two of its periods."""
    from modules.connectors.metric_catalog import DEFAULT_LOOKBACK_DAYS

    for provider, entry in metric_catalog().items():
        if not entry.charts_as_series:
            assert entry.cadence_days is None, provider
            continue
        assert entry.cadence_days is not None, provider
        assert (entry.default_lookback_days or DEFAULT_LOOKBACK_DAYS) >= 2 * entry.cadence_days, provider


def test_slow_series_declare_wide_defaults() -> None:
    """Annual World Bank and monthly OECD CLI start wider than the generic 90 days."""
    assert provider_metrics("world_bank").default_lookback_days == 3650
    assert provider_metrics("oecd_cli").default_lookback_days == 730
    assert provider_metrics("fred").default_lookback_days == 730
    assert provider_metrics("noaa_kp").default_lookback_days == 30
    assert provider_metrics("binance").default_lookback_days == 3


def test_observation_window_accepts_ten_years() -> None:
    """The shared observation window allows 3660 days and still rejects wider or inverted windows."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    import pytest
    from pydantic import ValidationError

    from modules.dashboard.schemas import GadgetScope
    from modules.knowledge.observations.schemas import ObservationQuery

    to = datetime(2026, 10, 1, tzinfo=UTC)
    base = {"source_ids": [uuid4()], "to_at": to}
    ObservationQuery(from_at=to - timedelta(days=3660), **base)
    with pytest.raises(ValidationError):
        ObservationQuery(from_at=to - timedelta(days=3661), **base)
    assert GadgetScope(lookback_days=3660).lookback_days == 3660
    with pytest.raises(ValidationError):
        GadgetScope(lookback_days=3661)


def test_fast_cadence_providers_default_to_a_window_that_fits_one_chart_fetch() -> None:
    """Sub-daily series default to a window whose points at the catalog cadence (15-minute pollers: 3 d = 288) fit the 500 rows the chart reads."""
    from math import ceil

    for provider, entry in metric_catalog().items():
        if entry.cadence_days is None or entry.cadence_days >= 1:
            continue
        assert entry.default_lookback_days is not None, provider
        assert 2 * entry.cadence_days <= entry.default_lookback_days, provider
        assert ceil(entry.default_lookback_days / entry.cadence_days) <= 500, provider


def test_quarter_hour_providers_match_their_default_poll_interval() -> None:
    """Catalog cadence of the 15-minute pollers equals provider_specs.default_interval_minutes."""
    for provider in ("binance", "coingecko", "coinpaprika", "iss_position"):
        assert get_provider_spec(provider).default_interval_minutes == 15, provider
        assert metric_catalog()[provider].cadence_days == pytest.approx(15 / 1440), provider
