"""Source wizard payload contract: what connector-editor.tsx sends must be accepted by the save/validate path.

Mirrors the frontend ``defaultConfiguration`` plus the smallest valid owner scope, then runs the same backend
steps as PUT /configuration and POST /validate-draft (request model, ``serialize_source_configuration``,
``registry.validate``). No database, network or Docker.
"""

from typing import Any
from uuid import uuid4

import pytest

from modules.connectors import registry
from modules.connectors.backends import GENERIC_CATALOG_SOURCE_TYPES, PROVIDER_SOURCE_TYPES
from modules.connectors.catalog import get_catalog_entry, list_catalog
from modules.connectors.provisioning_routes import ConnectorSettingsRequest
from modules.connectors.public import serialize_source_configuration
from modules.sources.schemas import ConnectorSource

# Smallest valid owner scope the wizard's "choose data" step can produce for each scoped provider.
MINIMAL_SCOPE: dict[str, dict[str, Any]] = {
    "youtube": {"youtube_channel_id": "UC" + "a" * 22},
    "arxiv": {"arxiv_category": "cs.AI"},
    "huggingface": {"huggingface_author": "openai"},
    "github": {"github_owner": "octocat", "github_repository": "hello"},
    "github_releases": {"github_owner": "octocat", "github_repository": "hello"},
    "telegram": {"telegram_chat_ids": ["-1001234567890"]},
    "alpha_vantage": {"market_symbols": ["IBM"], "market_currency": "USD", "market_exchange_timezone": "America/New_York"},
    "google_news": {"news_query": "economy"},
    "sec_edgar": {"sec_cik": "0000320193", "sec_contact_email": "owner@example.com"},
    "openalex": {"scholarly_query": "llm"},
    "fred": {"fred_series_id": "DFF"},
    "openweathermap": {"owm_latitude": 21.0, "owm_longitude": 105.8},
    "openaq": {"openaq_location_id": 8118},
    "nasa_firms": {"firms_country": "VNM"},
    "bluesky": {"bluesky_actor": "example.bsky.social"},
    "reddit": {"reddit_subreddit": "python"},
    "mastodon": {"mastodon_instance_url": "https://mastodon.social", "mastodon_account": "alice@mastodon.social"},
    "x": {"x_account": "example"},
    "google_mail": {"google_mail_label_ids": ["INBOX"]},
    "google_calendar": {"google_calendar_ids": ["primary"]},
    "google_drive": {"google_drive_ids": ["file-id"]},
    "health": {"fhir_base_url": "http://127.0.0.1:8080/fhir", "fhir_patient_id": "patient-1"},
    "home_assistant": {"ha_base_url": "http://127.0.0.1:8123", "ha_entity_ids": ["sensor.temperature"]},
    "notion": {"notion_page_ids": ["a1234567-b123-c123-d123-e123456789ab"]},
    "slack": {"slack_channel_ids": ["C012ABCDEF"]},
}
# open_meteo is omitted from SCOPED: the editor preselects 0/0 coordinates, UTC and a metric, so its default already validates.
SCOPED = frozenset(MINIMAL_SCOPE)
MINIMAL_SCOPE["open_meteo"] = {"weather_latitude": 21.0, "weather_longitude": 105.8, "weather_timezone": "UTC", "weather_metrics": ["temperature_2m"]}
GENERIC = {"rss": {"feed_url": "https://example.com/feed.xml"}, "web": {"url": "https://example.com/"},
           "rest": {"url": "https://example.com/api", "items_path": "items", "id_field": "id", "content_field": "content"}}


# Exactly the fields defaultConfiguration() clears per scoped preset (other example scope values stay preselected).
FRONTEND_CLEARED = {
    "google_news": ("news_query",), "sec_edgar": ("sec_cik", "sec_contact_email"), "openalex": ("scholarly_query",),
    "fred": ("fred_series_id",), "openweathermap": ("owm_latitude", "owm_longitude"), "openaq": ("openaq_location_id",),
    "nasa_firms": ("firms_country",),
}


def frontend_default(provider: str) -> dict[str, Any]:
    """Port of ``defaultConfiguration`` in connector-editor.tsx (scope fields cleared for scoped presets)."""
    entry = get_catalog_entry(provider)
    example = dict(entry.example_config or {}) if entry else {}
    if provider in PROVIDER_SOURCE_TYPES and example:  # free providers start from the catalog example
        for field in FRONTEND_CLEARED.get(provider, ()):
            example.pop(field, None)  # cleared ('' / undefined) by the editor; omitted from the JSON body
        return example
    if provider in PROVIDER_SOURCE_TYPES:  # native default branch
        base: dict[str, Any] = {
            "timeout_seconds": 30, "timezone": "Asia/Ho_Chi_Minh",
            "schedule_interval_minutes": 1440 if provider == "alpha_vantage" else 15 if provider in ("youtube", "arxiv") else 30,
            "history_mode": "pending_updates" if provider == "telegram" else "returned_snapshot",
        }
        if provider == "open_meteo":
            base |= {"weather_metrics": ["temperature_2m"], "weather_timezone": "UTC", "weather_latitude": 0, "weather_longitude": 0}
        if provider == "github":
            base |= {"include_issues": True, "include_pulls": True, "include_commits": True, "include_releases": True, "github_history_days": 90}
        return base
    return {"js_render": False, "max_pages": 10, "max_depth": 2, "timeout_seconds": 60, "timezone": "Asia/Ho_Chi_Minh",
            "schedule_interval_minutes": 15 if provider == "rss" else 30,
            **({"items_path": "items", "id_field": "id", "content_field": "content"} if provider == "rest" else {})}


def run_save_path(provider: str, configuration: dict[str, Any]) -> dict[str, Any]:
    """Run the request model, serializer and registry validation exactly as validate-draft and PUT do."""
    source_type = PROVIDER_SOURCE_TYPES.get(provider) or GENERIC_CATALOG_SOURCE_TYPES[provider]
    source = ConnectorSource(id=uuid4(), workspace_id=uuid4(), type=source_type, status="active", generation=1,
                             configuration={}, local_only=False, provider=provider if provider in PROVIDER_SOURCE_TYPES else None)
    payload = ConnectorSettingsRequest(expected_revision=0, configuration=configuration, auth_method="none")
    stored = serialize_source_configuration(source, payload.configuration)
    return registry.validate(source.model_copy(update={"configuration": stored}))


OFFERED_NATIVE = sorted(
    entry.provider_id for entry in list_catalog()
    if entry.provider_id in PROVIDER_SOURCE_TYPES and entry.availability in ("available", "implemented", "requires_credentials")
)


@pytest.mark.parametrize("provider", OFFERED_NATIVE)
def test_native_provider_with_minimal_scope_is_accepted(provider: str) -> None:
    """Every offered provider validates once the owner supplied its scope (fixed-scope ones need none)."""
    configuration = {**frontend_default(provider), **MINIMAL_SCOPE.get(provider, {})}
    assert run_save_path(provider, configuration)["provider"] == provider


@pytest.mark.parametrize("provider", sorted(GENERIC))
def test_generic_source_with_url_is_accepted(provider: str) -> None:
    """RSS, web and REST sources validate with their URL; they must never carry native-only history_mode."""
    configuration = {**frontend_default(provider), **GENERIC[provider]}
    assert "history_mode" not in configuration
    assert run_save_path(provider, configuration)["type"] == GENERIC_CATALOG_SOURCE_TYPES[provider]


@pytest.mark.parametrize("provider", sorted(SCOPED & set(OFFERED_NATIVE)))
def test_scope_is_required_before_validation_can_pass(provider: str) -> None:
    """Documents why the wizard's Connect step cannot validate: scope is only entered in the later Choose step."""
    with pytest.raises(ValueError):
        run_save_path(provider, frontend_default(provider))


@pytest.mark.parametrize("provider", sorted(GENERIC))
def test_generic_source_without_url_is_rejected(provider: str) -> None:
    """A generic source validated before its URL step is rejected, by design."""
    with pytest.raises(ValueError):
        run_save_path(provider, frontend_default(provider))
