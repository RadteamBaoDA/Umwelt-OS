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
    # Source ceiling for "multi" renderers (None = the generic selector ceiling of 32 applies).
    max_sources: int | None = None


@dataclass(frozen=True, slots=True)
class PresetSlot:
    """Name one stable preset slot and the renderer configuration it requests.

    ``label`` names the created gadget definition (else the renderer's display name). ``size`` is the
    preferred desktop (width, height) on the 20-column grid, never applied below the renderer minimum.
    ``providers`` restricts the automatic source proposal to these providers, in preference order, so a
    themed slot (security, jobs) never fills up with unrelated sources; empty means any compatible source.
    """

    slot_id: str
    renderer: str
    label: str | None = None
    size: tuple[int, int] | None = None
    providers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DashboardPreset:
    """Hold static preset identity, family, display label, and immutable slots."""

    id: str
    label: str
    family: str
    slots: tuple[PresetSlot, ...]


# Maps in presets are live (provider-driven, no bound sources): new maps default to live mode, and the
# scope must be seeded explicitly because an empty ``live_map_providers`` renders nothing.
LIVE_MAP_DEFAULT_PROVIDERS: tuple[str, ...] = (
    "usgs",
    "eonet",
    "hackernews",
    "lobsters",
    "cisa_kev",
    "nvd",
    "business",
    "fed",
)


def preset_slot_is_live(slot: PresetSlot) -> bool:
    """Return whether a preset slot is a live map, which takes seeded providers and never bound sources."""
    return slot.renderer == "map"


# Measurement renderers that may bind several same-kind datasources (one scope, union semantics).
MEASUREMENT_RENDERERS = frozenset({"finance_chart", "provider_observation", "weather"})
MAX_MEASUREMENT_SOURCES = 5

RENDERERS: tuple[RendererDescriptor, ...] = (
    RendererDescriptor(
        "news_feed",
        1,
        4,
        4,
        "available",
        ("news", "documents"),
        "multi",
        frozenset({"news", "event"}),
    ),
    RendererDescriptor(
        "telegram_feed",
        1,
        4,
        4,
        "available",
        ("telegram_messages",),
        "multi",
        frozenset({"messages"}),
    ),
    RendererDescriptor(
        "text_panel", 1, 4, 3, "available", ("documents",), "multi", frozenset({"news"})
    ),
    RendererDescriptor(
        "table_panel",
        1,
        6,
        4,
        "available",
        ("documents",),
        "multi",
        frozenset({"news", "measurement", "event"}),
    ),
    RendererDescriptor(
        "job_list", 1, 6, 4, "available", ("documents",), "multi", frozenset({"job"})
    ),
    RendererDescriptor(
        "video_panel", 1, 6, 4, "available", ("video",), "multi", frozenset({"video"})
    ),
    RendererDescriptor(
        "finance_chart",
        1,
        6,
        4,
        "available",
        ("market_series", "world_observations"),
        "multi",
        frozenset({"series"}),
        MAX_MEASUREMENT_SOURCES,
    ),
    RendererDescriptor("personal_context", 1, 4, 4, "planned", ("personal_context",)),
    RendererDescriptor(
        "map",
        1,
        8,
        6,
        "available",
        ("map_layers", "world_observations"),
        "multi",
        frozenset({"located"}),
    ),
    RendererDescriptor(
        "intelligence_panel",
        1,
        8,
        6,
        "available",
        ("cross_stream_correlation", "cii_v8_availability"),
        "multi",
        frozenset({"located"}),
    ),
    RendererDescriptor("highlights", 1, 4, 3, "available", ("highlights",), "multi"),
    RendererDescriptor("watch_rules", 1, 4, 3, "available", ("watch_rules",), "multi"),
    RendererDescriptor("tasks", 1, 4, 4, "available", ("tasks",)),
    RendererDescriptor("goals", 1, 4, 4, "available", ("goals",)),
    RendererDescriptor("daily_brief", 1, 6, 4, "available", ("daily_brief",)),
    RendererDescriptor(
        "weather",
        1,
        4,
        3,
        "available",
        ("weather", "world_observations"),
        "multi",
        frozenset({"weather"}),
        MAX_MEASUREMENT_SOURCES,
    ),
    # Generic provider observation gadget (Fear & Greed, FX, GDP, BTC price, USGS quakes).
    RendererDescriptor(
        "provider_observation",
        1,
        4,
        3,
        "available",
        ("world_observations",),
        "multi",
        frozenset({"measurement"}),
        MAX_MEASUREMENT_SOURCES,
    ),
    RendererDescriptor("research", 1, 4, 4, "planned", ("research",)),
    # Reads the GitHub project summary and Timeline slice of one configured github source.
    RendererDescriptor(
        "github_project",
        1,
        6,
        4,
        "available",
        ("github_project",),
        "single",
        frozenset({"project_activity"}),
    ),
)

PRESETS: tuple[DashboardPreset, ...] = (
    DashboardPreset(
        "overview",
        "Overview",
        "purpose",
        (
            PresetSlot("news_feed", "news_feed"),
            PresetSlot("map", "map"),
            PresetSlot("highlights", "highlights"),
            PresetSlot("daily_brief", "daily_brief"),
        ),
    ),
    DashboardPreset("technology", "Technology", "purpose", (PresetSlot("news_feed", "news_feed"),)),
    DashboardPreset(
        "finance",
        "Finance",
        "purpose",
        (
            PresetSlot("finance_chart", "finance_chart"),
            PresetSlot("news_feed", "news_feed"),
        ),
    ),
    DashboardPreset(
        "personal",
        "Personal",
        "purpose",
        (
            PresetSlot("tasks", "tasks"),
            PresetSlot("goals", "goals"),
            PresetSlot("daily_brief", "daily_brief"),
        ),
    ),
    DashboardPreset(
        "world",
        "World",
        "purpose",
        (
            PresetSlot("map", "map"),
            PresetSlot("news_feed", "news_feed"),
            PresetSlot("highlights", "highlights"),
            PresetSlot("daily_brief", "daily_brief"),
        ),
    ),
    DashboardPreset(
        "commodity",
        "Commodity",
        "variant",
        (
            PresetSlot("finance_chart", "finance_chart"),
            PresetSlot("news_feed", "news_feed"),
        ),
    ),
    DashboardPreset("happy", "Happy", "variant", (PresetSlot("news_feed", "news_feed"),)),
    DashboardPreset(
        "energy",
        "Energy",
        "variant",
        (
            PresetSlot("finance_chart", "finance_chart"),
            PresetSlot("news_feed", "news_feed"),
        ),
    ),
    # Situation-room layout over the starter-pack datasources: a wide map beside a tall live-news column,
    # then market strips, small metric tiles, and themed feeds. Rows pack to exactly 20 columns.
    DashboardPreset(
        "monitor",
        "Monitor",
        "purpose",
        (
            PresetSlot("situation_map", "map", "Situation map", (13, 10)),
            PresetSlot(
                "live_news",
                "news_feed",
                "Live news",
                (7, 10),
                ("bbc_world", "tuoitre_news", "vnexpress_business", "google_news"),
            ),
            PresetSlot("vn_market", "finance_chart", "VN market", (7, 6), ("vn_stock",)),
            PresetSlot(
                "crypto_market", "finance_chart", "Crypto", (7, 6), ("binance", "coinpaprika")
            ),
            PresetSlot(
                "fear_greed", "provider_observation", "Fear & Greed", (6, 6), ("alternative_me",)
            ),
            PresetSlot("usd_vnd", "provider_observation", "USD/VND", (5, 5), ("frankfurter",)),
            PresetSlot("macro", "provider_observation", "Macro", (5, 5), ("world_bank",)),
            PresetSlot("weather", "weather", "Weather", (5, 5), ("open_meteo",)),
            PresetSlot("highlights", "highlights", "Highlights", (5, 5)),
            PresetSlot(
                "tech_news", "news_feed", "Tech", (7, 8), ("hn_top", "lobsters", "github_trending")
            ),
            PresetSlot("security", "news_feed", "Security", (6, 8), ("cisa_kev", "nvd")),
            PresetSlot(
                "jobs", "job_list", "Jobs", (7, 8), ("vietnamworks_jobs", "itviec_jobs", "wwr_jobs")
            ),
            PresetSlot("daily_brief", "daily_brief", "Daily brief", (10, 7)),
            PresetSlot(
                "research",
                "news_feed",
                "Research & releases",
                (10, 7),
                ("arxiv", "github_releases", "fed_press"),
            ),
        ),
    ),
    # Topic pages: each tiles to exactly 20 columns per row (see default_desktop_layout) so a page can be
    # appended to an existing dashboard as its own group.
    DashboardPreset(
        "news",
        "News",
        "purpose",
        (
            PresetSlot("news_map", "map", "World map", (20, 8)),
            PresetSlot(
                "news_world",
                "news_feed",
                "World",
                (7, 10),
                ("bbc_world", "tuoitre_news", "vnexpress_business", "google_news"),
            ),
            PresetSlot(
                "news_tech",
                "news_feed",
                "Technology",
                (7, 10),
                ("hn_top", "lobsters", "github_trending"),
            ),
            PresetSlot("news_security", "news_feed", "Security", (6, 10), ("cisa_kev", "nvd")),
        ),
    ),
    DashboardPreset(
        "markets",
        "Markets",
        "purpose",
        (
            PresetSlot(
                "markets_kpis",
                "provider_observation",
                "Key indicators",
                (20, 3),
                ("frankfurter", "alternative_me", "world_bank"),
            ),
            PresetSlot("markets_vn", "finance_chart", "VN market", (7, 6), ("vn_stock",)),
            PresetSlot(
                "markets_crypto", "finance_chart", "Crypto", (7, 6), ("binance", "coinpaprika")
            ),
            PresetSlot("markets_macro", "provider_observation", "Macro", (6, 6), ("world_bank",)),
            PresetSlot(
                "markets_news",
                "news_feed",
                "Business news",
                (14, 8),
                ("vnexpress_business", "google_news", "fed_press"),
            ),
            PresetSlot("markets_highlights", "highlights", "Highlights", (6, 8)),
        ),
    ),
    DashboardPreset(
        "work",
        "Work",
        "purpose",
        (
            PresetSlot("work_tasks", "tasks", "Tasks", (14, 8)),
            PresetSlot("work_goals", "goals", "Goals", (6, 8)),
            PresetSlot("work_brief", "daily_brief", "Daily brief", (14, 7)),
            PresetSlot(
                "work_jobs",
                "job_list",
                "Jobs",
                (6, 7),
                ("vietnamworks_jobs", "itviec_jobs", "wwr_jobs"),
            ),
        ),
    ),
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
    GadgetTemplate("jobs", "job_list"),
    GadgetTemplate("video", "video_panel"),
    GadgetTemplate("chart", "finance_chart"),
    GadgetTemplate(
        "map",
        "map",
        scope={"live_map_providers": list(LIVE_MAP_DEFAULT_PROVIDERS)},
        filters={"map_mode": "live"},
    ),
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


# Preferred first-placement size (width x height, 20-column grid); never below the renderer minimum.
DEFAULT_SIZES: Mapping[str, tuple[int, int]] = {
    "news_feed": (6, 8),
    "telegram_feed": (6, 8),
    "text_panel": (6, 5),
    "table_panel": (10, 7),
    "video_panel": (8, 7),
    "finance_chart": (8, 6),
    "personal_context": (5, 5),
    "map": (12, 9),
    "intelligence_panel": (10, 7),
    "highlights": (5, 5),
    "watch_rules": (5, 5),
    "tasks": (5, 6),
    "goals": (5, 6),
    "daily_brief": (8, 7),
    "weather": (5, 5),
    "provider_observation": (5, 4),
    "research": (5, 5),
    "github_project": (8, 6),
    "job_list": (10, 8),
}


def renderer_default_size(renderer_id: str) -> tuple[int, int]:
    """Return the roomy (width, height) for a new gadget, falling back to the renderer minimum.

    Raises KeyError for unknown renderers. The result is clamped so it is never smaller than the
    minimum, which remains the only size used for layout validation.
    """
    descriptor = renderer_descriptor(renderer_id)
    width, height = DEFAULT_SIZES.get(renderer_id, (0, 0))
    return max(width, descriptor.minimum_width), max(height, descriptor.minimum_height)


def preset_slot_size(slot: PresetSlot) -> tuple[int, int]:
    """Return the slot's preferred (width, height), clamped up to the renderer minimum; renderer default when unset."""
    if slot.size is None:
        return renderer_default_size(slot.renderer)
    descriptor = renderer_descriptor(slot.renderer)
    return max(slot.size[0], descriptor.minimum_width), max(slot.size[1], descriptor.minimum_height)


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
        "map": {
            "regions",
            "map_layer_ids",
            "lookback_days",
            "live_map_providers",
            "live_map_country",
            "live_map_github_owner",
            "live_map_github_repo",
            "live_map_sec_cik",
        },
        "intelligence_panel": {"regions", "lookback_days", "cii_country_codes"},
    }.get(renderer_id, {"source_item_ids"})
    for field_name, values in configuration.scope.model_dump().items():
        if field_name not in allowed_scope and values:
            raise ValueError(f"{field_name} is not valid for renderer {renderer_id}")
    if renderer_id == "map":
        if set(configuration.scope.map_layer_ids) - {
            "world_observations",
            "military",
            "economic",
            "disaster",
            "escalation",
        }:
            raise GadgetConfigError(
                "Map layer is not in the bounded catalog",
                code="map_layer_unavailable",
                field="map_layer_ids",
            )
    if renderer_id == "intelligence_panel":
        if (
            configuration.scope.map_layer_ids
            or configuration.scope.metrics
            or configuration.scope.symbols
        ):
            raise ValueError(
                "Map and measurement selectors are not valid for the intelligence panel"
            )
    if renderer_id != "map" and (
        configuration.filters.map_engine is not None
        or configuration.filters.show_precise_locations
        or configuration.filters.map_mode is not None
        or configuration.filters.map_weather_layer is not None
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
    if renderer_id not in MEASUREMENT_RENDERERS:
        return
    fallback = provider is None
    catalog = provider_metrics(_FALLBACK_PROVIDER.get(renderer_id) if fallback else provider)
    if catalog is None:
        return
    metrics = scope.metrics
    if renderer_id == "finance_chart" and len(metrics) != 1 and not (fallback and not metrics):
        raise GadgetConfigError(
            "A chart needs exactly one metric", code="metric_not_in_catalog", field="metrics"
        )
    if not catalog.metrics_open and set(metrics) - {m.id for m in catalog.metrics}:
        raise GadgetConfigError(
            f"Metric is not emitted by provider {provider}",
            code="metric_not_in_catalog",
            field="metrics",
        )
    if fallback or renderer_id != "finance_chart":
        return
    if catalog.symbol_keyed and not scope.symbols:
        raise GadgetConfigError(
            f"Provider {provider} needs a symbol", code="symbol_required", field="symbols"
        )
    if catalog.region_keyed and not any(region.strip() for region in scope.regions):
        raise GadgetConfigError(
            f"Provider {provider} needs a region", code="region_required", field="regions"
        )


def validate_measurement_scopes(
    renderer_id: str, providers: Sequence[str | None], scope: GadgetScope
) -> None:
    """Check a measurement scope against every bound provider; the scope is one union over all sources.

    0 or 1 distinct providers delegate to ``validate_measurement_scope`` unchanged. With 2+ distinct providers
    each scope metric must be in the union of their catalogs (skipped when any provider is unknown or
    ``metrics_open``); a chart needs at least one metric and every closed-catalog provider must match exactly
    one scope metric; a symbol-keyed provider needs symbols and a region-keyed one a region. Raises
    ``GadgetConfigError`` with the same codes/fields as the single-provider rule.
    """
    distinct = list(dict.fromkeys(providers))
    if len(distinct) <= 1:
        validate_measurement_scope(renderer_id, distinct[0] if distinct else None, scope)
        return
    if renderer_id not in MEASUREMENT_RENDERERS:
        return
    catalogs = [provider_metrics(provider) for provider in distinct]
    metrics = scope.metrics
    closed = all(c is not None and not c.metrics_open for c in catalogs)
    if closed:
        union = {m.id for c in catalogs if c is not None for m in c.metrics}
        if set(metrics) - union:
            raise GadgetConfigError(
                "Metric is not emitted by any bound provider",
                code="metric_not_in_catalog",
                field="metrics",
            )
    if renderer_id == "finance_chart":
        # ponytail: symbols cannot be mapped to providers, so only metrics are matched per provider.
        if not metrics or any(
            c is not None
            and not c.metrics_open
            and sum(m in {x.id for x in c.metrics} for m in metrics) != 1
            for c in catalogs
        ):
            raise GadgetConfigError(
                "A chart needs exactly one metric per bound provider",
                code="metric_not_in_catalog",
                field="metrics",
            )
        if any(c is not None and c.symbol_keyed for c in catalogs) and not scope.symbols:
            raise GadgetConfigError(
                "A bound provider needs a symbol", code="symbol_required", field="symbols"
            )
        if any(c is not None and c.region_keyed for c in catalogs) and not any(
            r.strip() for r in scope.regions
        ):
            raise GadgetConfigError(
                "A bound provider needs a region", code="region_required", field="regions"
            )


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
    if (
        renderer_id not in {"finance_chart", "provider_observation"}
        or catalog is None
        or not catalog.metrics
    ):
        return {}
    if renderer_id == "finance_chart" and not catalog.charts_as_series:
        return {}
    if catalog.symbol_keyed and not (catalog.suggested_symbols and catalog.seed_symbol):
        return {}  # no knowable default symbol (none suggested or owner-configured): the editor asks for one
    scope: dict[str, list[str] | int] = {
        "metrics": [default_metric([metric.id for metric in catalog.metrics])]
    }
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
            max(catalog.default_lookback_days, 30)
            if renderer_id == "provider_observation"
            else catalog.default_lookback_days
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
    """Reject unknown preset slots, sources on live map slots, and mappings above the per-slot ceiling."""
    slots = {slot.slot_id: slot for slot in dashboard_preset(preset_id).slots}
    if not set(slot_sources).issubset(slots):
        raise ValueError("slot_sources contains an unknown preset slot")
    for slot_id, source_ids in slot_sources.items():
        if source_ids and preset_slot_is_live(slots[slot_id]):
            raise ValueError(f"slot {slot_id} is a live map and does not accept sources")
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
            raise GadgetConfigError(
                f"Renderer {renderer_id} does not accept data sources",
                code="source_incompatible",
                field="source_ids",
            )
        return
    source_limit = 1 if descriptor.source_mode == "single" else descriptor.max_sources
    if source_limit is not None and len(sources) > source_limit:
        raise GadgetConfigError(
            f"Renderer {renderer_id} accepts at most {source_limit} data source(s)",
            code="source_limit",
            field="source_ids",
        )
    if descriptor.accepted_shapes is not None and any(
        not source_data_shapes(provider, kind) & descriptor.accepted_shapes
        for provider, kind in sources
    ):
        raise GadgetConfigError(
            f"Renderer {renderer_id} only accepts sources producing: {', '.join(sorted(descriptor.accepted_shapes))}",
            code="source_incompatible",
            field="source_ids",
        )


def _check_templates() -> None:
    """Fail at import when a generic template names an unknown renderer or invalid default configuration."""
    for template in TEMPLATES:
        descriptor = _RENDERERS_BY_ID.get(template.renderer)
        if descriptor is None:
            raise ValueError(f"Template {template.id} uses unknown renderer {template.renderer}")
        validate_renderer_configuration(
            template.renderer,
            GadgetConfiguration.model_validate(
                {"scope": template.scope, "filters": template.filters}
            ),
        )


_check_templates()


def propose_slot_sources(
    renderer_id: str,
    active: Sequence[tuple[UUID, str | None, str]],
    *,
    multi_limit: int = 3,
    providers: Sequence[str] = (),
) -> list[UUID]:
    """Pick default source ids for one renderer from ``(id, provider, type)`` active-source tuples. Pure.

    ``none`` renderers bind nothing; ``single`` binds one, ``multi`` up to ``multi_limit``. Only sources whose
    data shapes intersect ``accepted_shapes`` qualify (None accepts all). With ``providers`` only those
    providers qualify, ranked in the given order (a themed slot stays unbound rather than take unrelated
    sources). Otherwise order is deterministic: sources with a provider rank before provider-less generic
    sources (rss/web/rest pages), then ``(provider or "", type, id)``.
    """
    descriptor = renderer_descriptor(renderer_id)
    if descriptor.source_mode == "none":
        return []
    accepted = descriptor.accepted_shapes
    candidates = [
        item
        for item in active
        if accepted is None or source_data_shapes(item[1], item[2]) & accepted
    ]
    if providers:
        order = {provider: index for index, provider in enumerate(providers)}
        ranked = sorted(
            (item for item in candidates if item[1] in order),
            key=lambda item: (order[item[1] or ""], item[2], str(item[0])),
        )
    else:
        ranked = sorted(
            candidates, key=lambda item: (item[1] is None, item[1] or "", item[2], str(item[0]))
        )
    # Preset seeding binds one provider's source to measurement renderers (their scope is single-provider).
    single = descriptor.source_mode == "single" or renderer_id in MEASUREMENT_RENDERERS
    return [item[0] for item in ranked[: 1 if single else multi_limit]]
