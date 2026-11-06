"""Live map settings remain bounded and cannot leak into unrelated renderers."""

import pytest
from pydantic import ValidationError

from modules.dashboard.gadgets import validate_renderer_configuration
from modules.dashboard.schemas import GadgetConfiguration, GadgetFilters, GadgetScope


def test_live_map_accepts_bounded_provider_configuration() -> None:
    config = GadgetConfiguration(
        scope=GadgetScope(
            live_map_providers=["usgs", "github_releases"],
            live_map_github_owner="openai",
            live_map_github_repo="example",
        ),
        filters=GadgetFilters(map_mode="live", map_weather_layer="wind"),
    )
    assert validate_renderer_configuration("map", config) == config


def test_legacy_map_has_no_implicit_live_mode() -> None:
    assert GadgetFilters().map_mode is None
    assert validate_renderer_configuration("map", GadgetConfiguration()).filters.map_mode is None


@pytest.mark.parametrize("value", ["../secret", "US", "usa", "USA/"])
def test_live_country_rejects_untrusted_scope(value: str) -> None:
    with pytest.raises(ValidationError):
        GadgetScope(live_map_country=value)


def test_live_options_cannot_be_applied_to_other_renderers() -> None:
    config = GadgetConfiguration(filters=GadgetFilters(map_mode="live"))
    with pytest.raises(ValueError):
        validate_renderer_configuration("tasks", config)


def test_live_provider_selector_rejects_unknown_or_duplicate() -> None:
    for providers in (["usgs", "usgs"], ["arbitrary-url"]):
        with pytest.raises(ValidationError):
            GadgetScope(live_map_providers=providers)
