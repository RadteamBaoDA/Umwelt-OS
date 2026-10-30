"""Static savable renderer metadata and source-free dashboard preset templates."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from modules.connectors.public import DataShape, provider_metrics, source_data_shapes
from modules.dashboard.schemas import GadgetConfiguration, GadgetScope


class GadgetConfigError(ValueError):
    """Gadget configuration rejection carrying a stable machine ``code`` and the offending ``field``."""

    def __init__(self, message: str, *, code: str, field: str | None = None) -> None:
        """Store the HTTP-visible code and field alongside the human message."""
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class RendererDescriptor:
    """Describe saved configuration, adapter presence and minimum geometry.

    ``available`` means a production data adapter exists; it does not certify runtime,
    provider, or deployment acceptance.
    """

    id: str
    config_version: int
    minimum_width: int
    minimum_height: int
    runtime_state: Literal["planned", "available"]
    capability_keys: tuple[str, ...]
    # How many sources the renderer binds and which data shapes they may emit (None = any source).
    source_mode: Literal["none", "single", "multi"] = "none"
    accepted_shapes: frozenset[DataShape] | None = None


@dataclass(frozen=True, slots=True)
class PresetSlot:
    """Name one stable preset slot and the renderer configuration it requests."""

    slot_id: str
    renderer: str


@dataclass(frozen=True, slots=True)
class DashboardPreset:
    """Hold static preset identity, family, display label, and immutable slots."""

    id: str
    label: str
    family: str
    slots: tuple[PresetSlot, ...]


RENDERERS: tuple[RendererDescriptor, ...] = (
    RendererDescriptor("news_feed", 1, 4, 4, "available", ("news", "documents"), "multi", frozenset({"news", "event"})),
    RendererDescriptor("telegram_feed", 1, 4, 4, "available", ("telegram_messages",), "multi", frozenset({"messages"})),
    RendererDescriptor("text_panel", 1, 4, 3, "available", ("documents",), "multi", frozenset({"news"})),
    RendererDescriptor("table_panel", 1, 6, 4, "available", ("documents",), "multi", frozenset({"news", "measurement", "event"})),
    RendererDescriptor("video_panel", 1, 6, 4, "available", ("video",), "multi", frozenset({"video"})),
    RendererDescriptor("finance_chart", 1, 6, 4, "available", ("market_series", "world_observations"), "single", frozenset({"series"})),
    RendererDescriptor("personal_context", 1, 4, 4, "planned", ("personal_context",)),
    RendererDescriptor("map", 1, 8, 6, "available", ("map_layers", "world_observations"), "multi", frozenset({"located"})),
    RendererDescriptor("intelligence_panel", 1, 8, 6, "available", ("cross_stream_correlation", "cii_v8_availability"), "multi", frozenset({"located"})),
    RendererDescriptor("highlights", 1, 4, 3, "available", ("highlights",), "multi"),
    RendererDescriptor("watch_rules", 1, 4, 3, "available", ("watch_rules",), "multi"),
    RendererDescriptor("tasks", 1, 4, 4, "available", ("tasks",)),
    RendererDescriptor("goals", 1, 4, 4, "available", ("goals",)),
    RendererDescriptor("daily_brief", 1, 6, 4, "available", ("daily_brief",)),
    RendererDescriptor("weather", 1, 4, 3, "available", ("weather", "world_observations"), "single", frozenset({"weather"})),
    # Generic provider observation gadget (Fear & Greed, FX, GDP, BTC price, USGS quakes).
    RendererDescriptor("provider_observation", 1, 4, 3, "available", ("world_observations",), "single", frozenset({"measurement"})),
    RendererDescriptor("research", 1, 4, 4, "planned", ("research",)),
    # Reads the GitHub project summary and Timeline slice of one configured github source.
    RendererDescriptor("github_project", 1, 6, 4, "available", ("github_project",), "single", frozenset({"project_activity"})),
)

PRESETS: tuple[DashboardPreset, ...] = (
    DashboardPreset("overview", "Overview", "purpose", (
        PresetSlot("news_feed", "news_feed"), PresetSlot("map", "map"),
        PresetSlot("highlights", "highlights"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("technology", "Technology", "purpose", (PresetSlot("news_feed", "news_feed"),)),
    DashboardPreset("finance", "Finance", "purpose", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
    DashboardPreset("personal", "Personal", "purpose", (
        PresetSlot("tasks", "tasks"), PresetSlot("goals", "goals"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("world", "World", "purpose", (
        PresetSlot("map", "map"), PresetSlot("news_feed", "news_feed"),
        PresetSlot("highlights", "highlights"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("commodity", "Commodity", "variant", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
    DashboardPreset("happy", "Happy", "variant", (PresetSlot("news_feed", "news_feed"),)),
    DashboardPreset("energy", "Energy", "variant", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
)

# Retired preset ids kept resolvable for old clients; no persisted column stores preset ids.
PRESET_ALIASES: Mapping[str, str] = {"tech": "technology", "finance_variant": "finance"}


@dataclass(frozen=True, slots=True)
class GadgetTemplate:
    """Pair a generic display template with its renderer, without provider-specific defaults."""

    id: str
    renderer: str
    scope: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)


TEMPLATES: tuple[GadgetTemplate, ...] = (
    GadgetTemplate("feed", "news_feed"),
    GadgetTemplate("message_list", "telegram_feed"),
    GadgetTemplate("text", "text_panel"),
    GadgetTemplate("table", "table_panel"),
    GadgetTemplate("video", "video_panel"),
    GadgetTemplate("chart", "finance_chart"),
    GadgetTemplate("map", "map"),
    GadgetTemplate("related_signals", "intelligence_panel"),
    GadgetTemplate("highlights", "highlights"),
    GadgetTemplate("watchlist", "watch_rules"),
    GadgetTemplate("tasks", "tasks"),
    GadgetTemplate("goals", "goals"),
    GadgetTemplate("daily_brief", "daily_brief"),
    GadgetTemplate("weather", "weather"),
    GadgetTemplate("metric", "provider_observation"),
    GadgetTemplate("activity", "github_project"),
)

_RENDERERS_BY_ID = {descriptor.id: descriptor for descriptor in RENDERERS}
_PRESETS_BY_ID = {preset.id: preset for preset in PRESETS}


def renderer_descriptor(renderer_id: str) -> RendererDescriptor:
    """Return known saved-configuration metadata or raise KeyError for unknown renderer IDs."""
    return _RENDERERS_BY_ID[renderer_id]


def validate_renderer_configuration(
    renderer_id: str, configuration: GadgetConfiguration
) -> GadgetConfiguration:
    """Reject selectors outside the renderer's narrow v1 scope while retaining validated filters.

    Source and item selectors remain configuration only; this function performs no source lookup,
    authorization, collector creation, or renderer execution. Map-only engine and precise-location
    preferences are rejected on other renderer IDs, and layer IDs must belong to the fixed catalog.
    """
    renderer_descriptor(renderer_id)
    allowed_scope = {
        "telegram_feed": {"channel_ids"},
        "finance_chart": {"symbols", "metrics", "regions", "lookback_days"},
        "weather": {"metrics", "lookback_days"},
        "provider_observation": {"metrics", "symbols", "lookback_days"},
        "map": {"regions", "map_layer_ids", "lookback_days"},
        "intelligence_panel": {"regions", "lookback_days", "cii_country_codes"},
    }.get(renderer_id, {"source_item_ids"})
    for field_name, values in configuration.scope.model_dump().items():
        if field_name not in allowed_scope and values:
            raise ValueError(f"{field_name} is not valid for renderer {renderer_id}")
    if renderer_id == "map":  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        if set(configuration.scope.map_layer_ids) - {
            "world_observations", "military", "economic", "disaster", "escalation",
        }:
            raise GadgetConfigError("Map layer is not in the bounded catalog",
                                    code="map_layer_unavailable", field="map_layer_ids")
    if renderer_id == "intelligence_panel":  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        if configuration.scope.map_layer_ids or configuration.scope.metrics or configuration.scope.symbols:
            raise ValueError("Map and measurement selectors are not valid for the intelligence panel")
    if renderer_id != "map" and (
        configuration.filters.map_engine is not None or configuration.filters.show_precise_locations
    ):
        raise ValueError("Map display preferences are only valid for the map renderer")
    return configuration


def default_metric(metric_ids: Sequence[str]) -> str:
    """Return the default chart metric for a provider's ordered metric ids: ``close`` when listed, else the first.

    Shared rule for preset seeding (the web editor mirrors it for new charts). ``metric_ids`` must be non-empty.
    """
    return "close" if "close" in metric_ids else metric_ids[0]


# Sourceless charts/weather gadgets still get an allowlist: the default provider each renderer is designed around.
_FALLBACK_PROVIDER = {"finance_chart": "alpha_vantage", "weather": "open_meteo"}


def validate_measurement_scope(renderer_id: str, provider: str | None, scope: GadgetScope) -> None:
    """Check chart/metric/weather scope against the bound provider's metric catalog.

    Applies to ``finance_chart``, ``provider_observation`` and ``weather``; other renderers and
    providers without a catalog entry pass. With no provider, ``finance_chart``/``weather`` fall back to the
    Alpha Vantage / Open-Meteo catalog for the metric allowlist only (symbol/region keys are not enforced).
    Raises ``GadgetConfigError`` with ``metric_not_in_catalog`` (field ``metrics``), ``symbol_required``
    (field ``symbols``) or ``region_required`` (field ``regions``, region-keyed providers need one region).
    Providers flagged ``metrics_open`` skip the membership check; the symbol/region rules still apply.
    """
    if renderer_id not in {"finance_chart", "provider_observation", "weather"}:
        return
    fallback = provider is None
    catalog = provider_metrics(_FALLBACK_PROVIDER.get(renderer_id) if fallback else provider)
    if catalog is None:
        return
    metrics = scope.metrics
    if renderer_id == "finance_chart" and len(metrics) != 1 and not (fallback and not metrics):
        raise GadgetConfigError("A chart needs exactly one metric", code="metric_not_in_catalog", field="metrics")
    if not catalog.metrics_open and set(metrics) - {m.id for m in catalog.metrics}:
        raise GadgetConfigError(f"Metric is not emitted by provider {provider}",
                                code="metric_not_in_catalog", field="metrics")
    if fallback or renderer_id != "finance_chart":
        return
    if catalog.symbol_keyed and not scope.symbols:
        raise GadgetConfigError(f"Provider {provider} needs a symbol", code="symbol_required", field="symbols")
    if catalog.region_keyed and not any(region.strip() for region in scope.regions):
        raise GadgetConfigError(f"Provider {provider} needs a region", code="region_required", field="regions")


def preset_slot_scope(renderer_id: str, provider: str | None) -> dict[str, list[str] | int]:
    """Return the starting scope for a preset slot bound to a single measurement ``provider``.

    Chart-like renderers (``finance_chart``/``provider_observation``) get ``default_metric`` and, for
    symbol-keyed providers whose symbols are fixed (``seed_symbol``), the first suggested symbol (and ``lookback_days`` seeded from the catalog default when it declares one, which may be narrower or wider than the generic window) (region-keyed ones the first suggested region for
    charts). Symbol-keyed providers without suggestions or with owner-configured symbols (Alpha Vantage, FRED, Binance), region-keyed charts without a
    suggested region, providers that do not chart as a series, other renderers, unbound slots and providers
    without a catalog entry return ``{}``: the chart then queries the default ``close`` metric, finds no rows and shows its
    setup message (a legacy Alpha Vantage chart with metrics ``[]`` and a symbol still plots ``close``). Pure; no lookup.
    """
    catalog = provider_metrics(provider)
    if renderer_id not in {"finance_chart", "provider_observation"} or catalog is None or not catalog.metrics:
        return {}
    if renderer_id == "finance_chart" and not catalog.charts_as_series:
        return {}
    if catalog.symbol_keyed and not (catalog.suggested_symbols and catalog.seed_symbol):
        return {}  # no knowable default symbol (none suggested or owner-configured): the editor asks for one
    scope: dict[str, list[str] | int] = {"metrics": [default_metric([metric.id for metric in catalog.metrics])]}
    if catalog.symbol_keyed:
        scope["symbols"] = [catalog.suggested_symbols[0]]
    if renderer_id == "finance_chart" and catalog.region_keyed:
        if not catalog.suggested_regions:
            return {}
        scope["regions"] = [catalog.suggested_regions[0]]
    if catalog.default_lookback_days:
        # Slow series (annual/monthly) need a wider window. A Metric slot never starts below its 30-day runtime
        # window (the 3-day default is a chart-point budget), so it takes the wider of the two.
        scope["lookback_days"] = (
            max(catalog.default_lookback_days, 30) if renderer_id == "provider_observation" else catalog.default_lookback_days
        )
    return scope


def dashboard_preset(preset_id: str) -> DashboardPreset:
    """Resolve a canonical or retired (``PRESET_ALIASES``) preset ID; KeyError when unknown.

    Touches neither persistence nor source data.
    """
    return _PRESETS_BY_ID[PRESET_ALIASES.get(preset_id, preset_id)]


def validate_preset_slot_sources(
    preset_id: str, slot_sources: Mapping[str, Sequence[UUID]]
) -> None:
    """Reject unknown preset slots and source mappings above the per-slot selector ceiling."""
    known_slots = {slot.slot_id for slot in dashboard_preset(preset_id).slots}
    if not set(slot_sources).issubset(known_slots):
        raise ValueError("slot_sources contains an unknown preset slot")
    for slot_id, source_ids in slot_sources.items():
        if len(source_ids) > 32 or len(source_ids) != len(set(source_ids)):
            raise ValueError(f"slot {slot_id} must contain at most 32 distinct source IDs")


def validate_renderer_sources(renderer_id: str, sources: Sequence[tuple[str | None, str]]) -> None:
    """Reject selected sources the renderer cannot display, by data shape.

    ``sources`` holds ``(provider, source_type)`` per selected source. A source is compatible when
    its shapes intersect the renderer's ``accepted_shapes`` (None accepts any). Raises
    ``GadgetConfigError`` with ``source_incompatible`` or ``source_limit`` on ``source_ids``;
    performs no lookup.
    """
    descriptor = renderer_descriptor(renderer_id)
    if descriptor.source_mode == "none":
        if sources:
            raise GadgetConfigError(f"Renderer {renderer_id} does not accept data sources",
                                    code="source_incompatible", field="source_ids")
        return
    if descriptor.source_mode == "single" and len(sources) > 1:
        raise GadgetConfigError(f"Renderer {renderer_id} accepts at most one data source",
                                code="source_limit", field="source_ids")
    if descriptor.accepted_shapes is not None and any(
        not source_data_shapes(provider, kind) & descriptor.accepted_shapes for provider, kind in sources
    ):
        raise GadgetConfigError(
            f"Renderer {renderer_id} only accepts sources producing: {', '.join(sorted(descriptor.accepted_shapes))}",
            code="source_incompatible", field="source_ids")


def _check_templates() -> None:
    """Fail at import when a generic template names an unknown renderer or invalid default configuration."""
    for template in TEMPLATES:
        descriptor = _RENDERERS_BY_ID.get(template.renderer)
        if descriptor is None:
            raise ValueError(f"Template {template.id} uses unknown renderer {template.renderer}")
        validate_renderer_configuration(template.renderer, GadgetConfiguration.model_validate(
            {"scope": template.scope, "filters": template.filters}))


_check_templates()
