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
