"""Declare which data shapes each connector provider emits, for gadget renderer compatibility.

Shapes are facts about adapter output, not runtime acceptance. ``located`` means the adapter
writes observations with non-None latitude/longitude (verified per adapter, not inferred from
event category); coordinates that appear only in free text do not count.
"""

from types import MappingProxyType
from typing import Literal

from modules.connectors.metric_catalog import provider_metrics
from modules.connectors.provider_specs import get_provider_spec

DataShape = Literal["news", "measurement", "event", "located", "messages", "video", "project_activity", "weather", "series"]

# Providers outside the free-provider catalog (no FreeProviderSpec); health/home_assistant adapters
# emit no metric observations, so they stay `news`.
_NON_CATALOG: MappingProxyType[str, frozenset[DataShape]] = MappingProxyType({
    "telegram": frozenset({"messages"}),
    "youtube": frozenset({"video"}),
    "github": frozenset({"project_activity"}),
    "open_meteo": frozenset({"measurement", "weather", "located"}),  # writes the configured latitude/longitude
})
# Added on top of FreeProviderSpec.data_kind: adapters that write latitude/longitude and/or a numeric metric.
_EXTRA: MappingProxyType[str, frozenset[DataShape]] = MappingProxyType({
    "usgs": frozenset({"located", "measurement"}),  # earthquake_magnitude + coordinates
    "nasa_eonet": frozenset({"located"}),  # coordinates nullable per event
    "openweathermap": frozenset({"located"}),
    "openaq": frozenset({"located"}),  # coordinates nullable per location
    "nasa_firms": frozenset({"located", "measurement"}),  # fire_radiative_power at hotspot lat/lon
    "iss_position": frozenset({"located"}),
})


def source_data_shapes(provider: str | None, source_type: str) -> frozenset[DataShape]:
    """Return the data shapes a source of ``provider`` can produce; always non-empty.

    ``provider`` None (custom RSS/web/REST/manual) and providers without a declared shape
    default to ``news``. ``source_type`` is accepted for signature stability and does not
    currently change the answer.
    """
    if provider is None:
        return frozenset({"news"})
    base: frozenset[DataShape]
    if provider in _NON_CATALOG:
        base = _NON_CATALOG[provider]
    else:
        spec = get_provider_spec(provider)
        kind: frozenset[DataShape] = frozenset({spec.data_kind}) if spec else frozenset({"news"})
        base = kind | _EXTRA.get(provider, frozenset())
    # ``series``: measurement rows that plot as one time series (not point events like quakes/hotspots).
    catalog = provider_metrics(provider)
    if "measurement" in base and catalog is not None and catalog.charts_as_series:
        base = base | frozenset({"series"})
    return base
