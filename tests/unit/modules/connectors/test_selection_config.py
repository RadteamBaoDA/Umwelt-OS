"""Config bounds and per-provider allowlist for the built-in selection options."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.connectors.public import PROVIDER_SOURCE_TYPES, ConnectorConfig
from modules.connectors.registry import validate
from modules.connectors.selection import SelectionFilters, selection_filters
from modules.sources.schemas import ConnectorSource

BASE = {"history_mode": "returned_snapshot", "schedule_interval_minutes": 30, "timeout_seconds": 30}


def _src(provider: str, configuration: dict) -> ConnectorSource:
    """Build an active built-in source row carrying ``configuration``."""
    return ConnectorSource(
        id=uuid4(),
        workspace_id=uuid4(),
        type=PROVIDER_SOURCE_TYPES[provider],
        status="active",
        generation=1,
        configuration=configuration,
        local_only=False,
        provider=provider,
    )


def test_filter_fields_accepted_for_incremental_builtin():
    validate(
        _src(
            "tuoitre_news",
            {"max_items": 20, "include_keywords": ["giá vàng"], "max_age_days": 3, **BASE},
        )
    )


def test_filter_fields_rejected_for_measurement_builtin():
    with pytest.raises(ValueError, match="unsupported fields"):
        validate(_src("binance", {"max_items": 5, **BASE}))


@pytest.mark.parametrize(
    "bad",
    [
        {"max_items": 0},
        {"max_items": 101},
        {"max_age_days": 366},
        {"include_keywords": ["x" * 65]},
        {"include_keywords": ["k"] * 21},
        {"exclude_keywords": [" "]},
    ],
)
def test_filter_bounds(bad):
    with pytest.raises(ValidationError):
        ConnectorConfig(**bad)


def test_empty_keyword_list_normalises_to_none():
    assert ConnectorConfig(include_keywords=[]).include_keywords is None


def test_selection_filters_defaults_to_today():
    assert selection_filters({}) == SelectionFilters(max_items=100)


def test_measurement_providers_are_not_incremental_and_reject_filters():
    from modules.connectors.provider_specs import INCREMENTAL_PROVIDERS
    from modules.knowledge.documents.schemas import MEASUREMENT_PROVIDER_IDS

    assert not INCREMENTAL_PROVIDERS & set(MEASUREMENT_PROVIDER_IDS)
    for provider in (
        "cisa_kev",
        "ioda",
        "nasa_eonet",
        "usgs",
    ):  # nvd alone takes max_items (its page size)
        with pytest.raises(ValueError):
            validate(_src(provider, {"max_items": 5, **BASE}))


def test_generic_rss_source_rejects_filter_fields():
    from modules.connectors.public import serialize_source_configuration

    src = ConnectorSource(
        id=uuid4(),
        workspace_id=uuid4(),
        type="rss",
        status="active",
        generation=1,
        local_only=False,
        configuration={},
        provider=None,
    )
    validate(
        src.model_copy(update={"configuration": {"feed_url": "https://example.com/feed.xml"}})
    )  # control
    config = ConnectorConfig(feed_url="https://example.com/feed.xml", max_items=5)
    with pytest.raises(ValueError):
        serialized = serialize_source_configuration(src, config)
        validate(src.model_copy(update={"configuration": serialized}))


def test_media_and_youtube_connector_config_defaults_and_bounds():
    from modules.connectors.builtins import BuiltinEnableRequest
    from modules.connectors.public import MEDIA_OPTION_NAMES

    defaults = ConnectorConfig()
    dumped = defaults.model_dump(mode="json", exclude_none=True)
    for field in (*MEDIA_OPTION_NAMES, "youtube_handles", "youtube_playlist_ids", "youtube_filter"):
        assert field not in dumped

    cfg = ConnectorConfig(
        media_country="TW",
        media_countries=("JP", "KR"),
        media_genres=("action", "sci_fi"),
        media_languages=("ja", "ko"),
        media_formats=("tv", "manhwa"),
        release_year=2026,
        anime_season="fall",
        content_ratings=("pg_13",),
        min_rating=75.5,
        exclude_adult=True,
        youtube_handles=("@veritasium",),
        youtube_playlist_ids=("UULF1234567890",),
        youtube_filter="long_form",
    )
    assert cfg.media_country == "TW" and cfg.youtube_filter == "long_form"

    builtin = BuiltinEnableRequest(
        media_countries=("US", "HK"),
        media_genres=("drama",),
        min_rating=80.0,
        exclude_adult=True,
    )
    assert builtin.media_countries == ("US", "HK") and builtin.min_rating == 80.0

    for bad in (
        {"min_rating": -0.1},
        {"min_rating": 100.1},
        {"min_rating": float("nan")},
        {"min_rating": True},
        {"release_year": 1799},
        {"release_year": 2101},
        {"anime_season": "monsoon"},
        {"media_genres": ("unknown_genre",)},
        {"youtube_filter": "clips"},
    ):
        with pytest.raises(ValidationError):
            ConnectorConfig(**bad)

    for bad_builtin in (
        {"min_rating": float("inf")},
        {"min_rating": True},
        {"release_year": 1700},
        {"media_genres": ("bad",)},
    ):
        with pytest.raises(ValidationError):
            BuiltinEnableRequest(**bad_builtin)


def test_generic_sources_reject_media_and_youtube_options():
    from modules.connectors.public import MEDIA_OPTION_NAMES

    for source_type, base_cfg in (
        ("rss", {"feed_url": "https://example.com/feed.xml"}),
        ("api", {"url": "https://example.com/items.json", "items_path": "items"}),
    ):
        for option, value in (
            *(
                (
                    k,
                    True
                    if k == "exclude_adult"
                    else 2026
                    if k == "release_year"
                    else 80.0
                    if k == "min_rating"
                    else "fall"
                    if k == "anime_season"
                    else "US"
                    if k == "media_country"
                    else ["action"]
                    if k == "media_genres"
                    else ["ja"]
                    if k == "media_languages"
                    else ["tv"]
                    if k == "media_formats"
                    else ["safe"]
                    if k == "content_ratings"
                    else ["JP"],
                )
                for k in sorted(MEDIA_OPTION_NAMES)
            ),
            ("youtube_handles", ["@chan"]),
            ("youtube_playlist_ids", ["PL123"]),
            ("youtube_filter", "shorts"),
        ):
            src = ConnectorSource(
                id=uuid4(),
                workspace_id=uuid4(),
                type=source_type,
                status="active",
                generation=1,
                local_only=False,
                configuration={**base_cfg, option: value},
                provider=None,
            )
            with pytest.raises(ValueError, match="registered source provider"):
                validate(src)
