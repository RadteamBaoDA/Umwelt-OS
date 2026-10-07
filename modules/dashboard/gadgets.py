"""Static savable renderer metadata and source-free dashboard preset templates."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from modules.dashboard.schemas import GadgetConfiguration


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
    RendererDescriptor("news_feed", 1, 4, 4, "available", ("news", "documents")),
    RendererDescriptor("telegram_feed", 1, 4, 4, "available", ("telegram_messages",)),
    RendererDescriptor("text_panel", 1, 4, 3, "available", ("documents",)),
    RendererDescriptor("table_panel", 1, 6, 4, "available", ("documents",)),
    RendererDescriptor("video_panel", 1, 6, 4, "available", ("video",)),
    RendererDescriptor("finance_chart", 1, 6, 4, "available", ("market_series", "world_observations")),
    RendererDescriptor("personal_context", 1, 4, 4, "planned", ("personal_context",)),
    RendererDescriptor("map", 1, 8, 6, "available", ("map_layers", "world_observations")),
    RendererDescriptor("intelligence_panel", 1, 8, 6, "available", ("cross_stream_correlation", "cii_v8_availability")),
    RendererDescriptor("highlights", 1, 4, 3, "available", ("highlights",)),
    RendererDescriptor("watch_rules", 1, 4, 3, "available", ("watch_rules",)),
    RendererDescriptor("tasks", 1, 4, 4, "available", ("tasks",)),
    RendererDescriptor("goals", 1, 4, 4, "available", ("goals",)),
    RendererDescriptor("daily_brief", 1, 6, 4, "available", ("daily_brief",)),
    RendererDescriptor("weather", 1, 4, 3, "available", ("weather", "world_observations")),
    RendererDescriptor("research", 1, 4, 4, "planned", ("research",)),
    # Reads the GitHub project summary and Timeline slice of one configured github source.
    RendererDescriptor("github_project", 1, 6, 4, "available", ("github_project",)),
)

PRESETS: tuple[DashboardPreset, ...] = (
    DashboardPreset("overview", "Overview", "purpose", (
        PresetSlot("news_feed", "news_feed"), PresetSlot("map", "map"),
        PresetSlot("highlights", "highlights"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("technology", "Technology", "purpose", (
        PresetSlot("news_feed", "news_feed"), PresetSlot("research", "research"),
    )),
    DashboardPreset("finance", "Finance", "purpose", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
    DashboardPreset("personal", "Personal", "purpose", (
        PresetSlot("personal_context", "personal_context"), PresetSlot("tasks", "tasks"),
        PresetSlot("goals", "goals"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("world", "World", "purpose", (
        PresetSlot("map", "map"), PresetSlot("news_feed", "news_feed"),
        PresetSlot("highlights", "highlights"), PresetSlot("daily_brief", "daily_brief"),
    )),
    DashboardPreset("tech", "Tech", "variant", (
        PresetSlot("news_feed", "news_feed"), PresetSlot("research", "research"),
        PresetSlot("highlights", "highlights"),
    )),
    DashboardPreset("finance_variant", "Finance", "variant", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
    DashboardPreset("commodity", "Commodity", "variant", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
    )),
    DashboardPreset("happy", "Happy", "variant", (PresetSlot("news_feed", "news_feed"),)),
    DashboardPreset("energy", "Energy", "variant", (
        PresetSlot("finance_chart", "finance_chart"), PresetSlot("news_feed", "news_feed"),
        PresetSlot("research", "research"),
    )),
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
        "finance_chart": {"symbols", "metrics", "lookback_days"},
        "weather": {"metrics", "lookback_days"},
        "map": {"regions", "map_layer_ids", "lookback_days"},
        "intelligence_panel": {"regions", "lookback_days", "cii_country_codes"},
    }.get(renderer_id, {"source_item_ids"})
    for field_name, values in configuration.scope.model_dump().items():
        if field_name not in allowed_scope and values:
            raise ValueError(f"{field_name} is not valid for renderer {renderer_id}")
    if renderer_id == "finance_chart" and set(configuration.scope.metrics) - {"open", "high", "low", "close", "volume"}:
        raise ValueError("Finance chart metric is outside the provider-declared daily OHLCV set")
    if renderer_id == "weather" and set(configuration.scope.metrics) - {"temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"}:
        raise ValueError("Weather metric is outside the Open-Meteo allowlist")
    if renderer_id == "map":  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        if set(configuration.scope.map_layer_ids) - {
            "world_observations", "military", "economic", "disaster", "escalation",
        }:
            raise ValueError("Map layer is not in the bounded catalog")
    if renderer_id == "intelligence_panel":  # noqa: SIM102  # style-only rewrite skipped to avoid touching control flow
        if configuration.scope.map_layer_ids or configuration.scope.metrics or configuration.scope.symbols:
            raise ValueError("Map and measurement selectors are not valid for the intelligence panel")
    if renderer_id != "map" and (
        configuration.filters.map_engine is not None or configuration.filters.show_precise_locations
    ):
        raise ValueError("Map display preferences are only valid for the map renderer")
    return configuration


def dashboard_preset(preset_id: str) -> DashboardPreset:
    """Resolve one of the ten fixed template IDs without accessing persistence or source data."""
    return _PRESETS_BY_ID[preset_id]


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
