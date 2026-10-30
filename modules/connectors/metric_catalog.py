"""Static catalog of the numeric metrics each measurement provider's adapter can emit.

Ids and symbols mirror what ``modules/connectors/providers/*.py`` write to ``metric=`` / ``symbol=``.
Gadget scope validation and pickers read this; it is not runtime acceptance. Providers whose
metric set is open-ended (configurable FRED series / World Bank indicators / OpenAQ parameters)
list only their default or common ids.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class MetricInfo:
    """One emitted metric id with its unit (None when the unit varies per source configuration)."""

    id: str
    unit: str | None


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    """Metrics a provider emits; ``symbol_keyed`` means observations are further split by ``symbol``."""

    metrics: tuple[MetricInfo, ...]
    symbol_keyed: bool
    suggested_symbols: tuple[str, ...] = ()
    # Open-ended providers (configurable indicators/series/parameters): listed metrics are suggestions only.
    metrics_open: bool = False
    # False for point-event providers whose rows are not one time series (each quake/hotspot has its own region).
    charts_as_series: bool = True
    # True where one source emits several regions as separate series (the region picks the series).
    region_keyed: bool = False
    suggested_regions: tuple[str, ...] = ()
    # False where the owner configures which symbols the source emits (fred series id, binance/coingecko
    # pairs/assets): the suggestions stay pickable hints but presets must not seed them as the scope.
    seed_symbol: bool = True
    # Typical spacing of one series' observations, in days (from the adapters). None for point-event providers.
    cadence_days: float | None = None
    # Starting chart window when a gadget does not set one; None means the generic 90 days (DEFAULT_LOOKBACK_DAYS).
    # Must cover at least two observation periods, otherwise a slow series draws no line.
    default_lookback_days: int | None = None


DEFAULT_LOOKBACK_DAYS = 90
HOURLY = 1 / 24
QUARTER_HOURLY = 15 / 1440  # the 15-minute default poll interval of provider_specs

def _m(*items: tuple[str, str | None]) -> tuple[MetricInfo, ...]:
    """Build a metric tuple from ``(id, unit)`` pairs."""
    return tuple(MetricInfo(i, u) for i, u in items)


# Pairs exactly as macro.OPEN_ER_QUOTES makes the adapter write them. ECB publishes ~30 quotes; its hints are the majors.
OPEN_ER_PAIRS = tuple(f"USD/{q}" for q in ("VND", "EUR", "JPY", "GBP", "CNY"))
_SPOT = _m(("spot_price", None))
_FX = _m(("fx_reference_rate", None))

_CATALOG: Mapping[str, ProviderMetrics] = MappingProxyType({
    # Unit is the configured market currency (volume: shares).
    "alpha_vantage": ProviderMetrics(
        _m(("open", None), ("high", None), ("low", None), ("close", None), ("volume", "shares")), True,
        cadence_days=1),
    "open_meteo": ProviderMetrics(_m(
        ("temperature_2m", None), ("relative_humidity_2m", None), ("precipitation", None), ("wind_speed_10m", None),
    ), False, cadence_days=HOURLY, default_lookback_days=3),
    "alternative_me": ProviderMetrics(_m(("fear_greed_index", "index (0-100)")), False, cadence_days=1),
    "binance": ProviderMetrics(_SPOT, True, ("BTCUSDT",), seed_symbol=False, cadence_days=QUARTER_HOURLY, default_lookback_days=3),  # the pair, as crypto.map_binance writes it
    "coinpaprika": ProviderMetrics(_SPOT, True, ("BTC",), cadence_days=QUARTER_HOURLY, default_lookback_days=3),
    "coingecko": ProviderMetrics(_SPOT, True, ("BTC",), seed_symbol=False, cadence_days=QUARTER_HOURLY, default_lookback_days=3),  # owner-configured assets
    "frankfurter": ProviderMetrics(_FX, True, ("USD/VND",), cadence_days=1),
    "ecb": ProviderMetrics(_FX, True, ("EUR/USD", "EUR/JPY", "EUR/GBP"), cadence_days=1),
    "open_er_api": ProviderMetrics(_FX, True, OPEN_ER_PAIRS, cadence_days=1),
    "noaa_kp": ProviderMetrics(_m(("kp_index", "kp")), False, cadence_days=0.125, default_lookback_days=30),
    # One line per country: map_sdmx_json writes the REF_AREA as symbol for the five requested areas.
    "oecd_cli": ProviderMetrics(_m(("composite_leading_indicator", "index")), True, ("USA", "CHN", "JPN", "DEU", "GBR"),
                            cadence_days=31, default_lookback_days=730),
    "iss_position": ProviderMetrics(_m(("iss_altitude", "km")), False, cadence_days=QUARTER_HOURLY, default_lookback_days=3),
    # map_usgs writes the quake place as region per event: points, not a series.
    "usgs": ProviderMetrics(_m(("earthquake_magnitude", "magnitude")), False, charts_as_series=False),
    # Hotspot rows carry the country as region and each hotspot is a separate point.
    "nasa_firms": ProviderMetrics(_m(("fire_radiative_power", "MW")), False, charts_as_series=False),
    "disease_sh": ProviderMetrics(_m(
        ("covid19_cases", "persons"), ("covid19_deaths", "persons"),
        ("covid19_recovered", "persons"), ("covid19_active", "persons"),
    ), False, cadence_days=1),
    "openweathermap": ProviderMetrics(_m(
        ("temperature", "degC"), ("humidity", "%"), ("pressure", "hPa"), ("wind_speed", "m/s"),
    ), False, cadence_days=HOURLY, default_lookback_days=3),
    # Open-ended: the metric id is the owner-configured indicator name; default only. The adapter fetches one
    # country (Viet Nam, macro.py), so it is one series, not region keyed.
    "world_bank": ProviderMetrics(_m(("gdp_current_usd", "current US$")), False, metrics_open=True,
                              cadence_days=365, default_lookback_days=3650),
    # keyed.map_fred writes metric "fred_series" with the series id as symbol; suggestions are common series.
    "fred": ProviderMetrics(_m(("fred_series", "series units")), True, ("DFF", "CPIAUCSL", "DGS10"),
                            seed_symbol=False, cadence_days=31, default_lookback_days=730),
    # Open-ended: id is OpenAQ's parameter name (units vary per sensor); common pollutants listed.
    "openaq": ProviderMetrics(_m(
        ("pm25", None), ("pm10", None), ("o3", None), ("no2", None), ("so2", None), ("co", None),
    ), False, metrics_open=True, region_keyed=True,
       cadence_days=HOURLY, default_lookback_days=3),  # keyed.map_openaq: region = "location <id>" (owner-configured)
})


def provider_metrics(provider: str | None) -> ProviderMetrics | None:
    """Return the metric entry for ``provider``, or None for None/unknown/non-measurement providers."""
    return _CATALOG.get(provider) if provider else None


def metric_catalog() -> Mapping[str, ProviderMetrics]:
    """Return the read-only provider -> metrics mapping."""
    return _CATALOG
