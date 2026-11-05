from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from modules.connectors.backends import (
    GENERIC_SOURCE_TYPES,
    NATIVE_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
    PROVIDER_SOURCE_TYPES,
    is_native_provider,
)
from modules.connectors.provider_options import ALL_OPTION_NAMES, option_fields, validate_options
from modules.connectors.provider_specs import (
    COLLECTION_FILTER_FIELDS,
    INCREMENTAL_PROVIDERS,
    min_interval_minutes,
)
from modules.connectors.public import DEFAULT_TIMEZONE, OPTIONAL_SCOPE_FIELDS, ConnectorConfig
from modules.connectors.sections import SECTION_CATALOG, validate_sections
from modules.sources.schemas import ConnectorSource

SUPPORTED_TYPES = GENERIC_SOURCE_TYPES - {"mcp"} | set(PROVIDER_SOURCE_TYPES.values())


def configuration(source: ConnectorSource) -> ConnectorConfig:
    """Validate settings and apply the 30-second default only to native sources.

    Generic connectors retain their historical 60-second default. Native
    sources with an omitted stored timeout use the provider's 30-second bound.
    """
    raw_configuration = dict(source.configuration or {})
    if source.provider is not None:
        # An omitted interval means "slowest allowed" for providers whose floor exceeds the generic default.
        floor = min_interval_minutes(source.provider)
        if floor > 30:
            raw_configuration.setdefault("schedule_interval_minutes", floor)
    config = ConnectorConfig.model_validate(raw_configuration)
    if is_native_provider(source.provider) and "timeout_seconds" not in (source.configuration or {}):
        config = config.model_copy(update={"timeout_seconds": 30})
    return config


def validate(source: ConnectorSource) -> dict[str, Any]:
    """Validate packaged settings and exact provider scopes before returning a collector snapshot.

    FHIR and Home Assistant require their configured endpoint and explicit patient/entity scope;
    endpoint syntax is checked here, while the local transport enforces resolved private addresses.
    """
    if source.status != "active":
        raise ValueError("Source is not active")
    if source.type == "mcp":
        from modules.connectors.mcp import validate as validate_mcp

        mcp_config = validate_mcp(source)
        return {
            "source_id": str(source.id), "source_generation": source.generation,
            "type": "mcp", "timezone": mcp_config.timezone,
            "configuration": mcp_config.model_dump(mode="json"),
        }
    if source.type not in SUPPORTED_TYPES:
        raise ValueError("This source type has no packaged connector")
    if source.provider is not None:
        return _validate_provider_source(source, source.provider)
    return _validate_generic_source(source)


# Provider -> (predicate that is True when the scope is INCOMPLETE, error message). A provider
# with extra mandatory scope adds one row here; ``_validate_provider_source`` never changes.
_PROVIDER_SCOPE_RULES: dict[str, tuple[Callable[[ConnectorConfig], bool], str]] = {
    "github_releases": (
        lambda c: not c.github_owner or not c.github_repository, "GitHub owner and repository are required"),
    "bluesky": (lambda c: not c.bluesky_actor, "A Bluesky handle or DID is required"),
    "mastodon": (
        lambda c: bool(c.mastodon_account) == bool(c.mastodon_hashtag),
        "Choose exactly one Mastodon account or hashtag"),
    "x": (lambda c: bool(c.x_account) == bool(c.x_query), "Choose exactly one X account or search query"),
    "alpha_vantage": (
        lambda c: not c.market_symbols or not c.market_currency or not c.market_exchange_timezone,
        "Alpha Vantage symbols, currency, and exchange timezone are required"),
    "open_meteo": (
        lambda c: (c.weather_latitude is None or c.weather_longitude is None
                   or not c.weather_timezone or not c.weather_metrics),
        "Open-Meteo coordinates, timezone, and metric scope are required"),
    "health": (lambda c: not c.fhir_base_url or not c.fhir_patient_id, "FHIR endpoint and patient ID are required"),
    "home_assistant": (
        lambda c: not c.ha_base_url or not c.ha_entity_ids,
        "Home Assistant endpoint and selected entities are required"),
}


def _check_provider_keys(source: ConnectorSource, provider: str) -> None:
    """Reject unsupported configuration keys and name every missing required scope key.

    Raises ``ValueError`` listing missing schema keys (never values) so the editor can point the
    owner at each empty field; ``history_mode`` must match the provider's receive mode.
    """
    scope_fields = PROVIDER_SCOPE_FIELDS[provider]
    common = {
        "timezone", "schedule_interval_minutes", "timeout_seconds",
        "realtime_mode", "retention_days", "skip_embedding",
    }
    expected_history = "pending_updates" if provider == "telegram" else "returned_snapshot"
    raw_keys = set(source.configuration or {})
    allowed = scope_fields | common | {"history_mode"}
    if provider in INCREMENTAL_PROVIDERS:
        allowed |= COLLECTION_FILTER_FIELDS
    if provider in SECTION_CATALOG:
        allowed |= {"feed_sections"}
    allowed |= option_fields(provider)
    if raw_keys - allowed:
        raise ValueError("Provider configuration contains unsupported fields")
    optional_scope_fields = OPTIONAL_SCOPE_FIELDS.get(provider, frozenset())
    required_scope_fields = scope_fields - optional_scope_fields
    # Name the missing schema keys (never values) so the editor can point the owner at each empty field.
    missing = sorted(required_scope_fields - raw_keys)
    if source.configuration.get("history_mode") != expected_history:
        missing.append("history_mode")
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")


def _validate_provider_source(source: ConnectorSource, provider: str) -> dict[str, Any]:
    """Validate a registered native provider source and return its collector snapshot.

    Order: registration and type match, key allow-list, section/option validation, timeout and
    interval floor, then the provider's mandatory-scope rule (``_PROVIDER_SCOPE_RULES``) and the
    GitHub config projection. Pure; raises ``ValueError``.
    """
    if provider not in NATIVE_PROVIDERS:
        raise ValueError("Provider is not registered")
    if source.type != PROVIDER_SOURCE_TYPES[provider]:
        raise ValueError("Source type does not match the registered provider")
    _check_provider_keys(source, provider)
    config = configuration(source)
    if config.feed_sections is not None:
        validate_sections(provider, config.feed_sections)
    validate_options(provider, config.model_dump(exclude_none=True))
    if config.timeout_seconds > 30:
        raise ValueError("Native provider request timeout cannot exceed 30 seconds")
    floor = min_interval_minutes(provider)
    if config.schedule_interval_minutes is not None and config.schedule_interval_minutes < floor:
        raise ValueError(f"schedule_interval_below_minimum: minimum {floor} minutes")
    rule = _PROVIDER_SCOPE_RULES.get(provider)
    if rule is not None and rule[0](config):
        raise ValueError(rule[1])
    if provider == "github":
        from modules.connectors.github.schemas import project_github_source_config

        project_github_source_config(config.model_dump(exclude_none=True))
    return {
        "source_id": str(source.id),
        "source_generation": source.generation,
        "type": source.type,
        "provider": provider,
        "timezone": config.timezone or DEFAULT_TIMEZONE,
        "configuration": config.model_dump(mode="json", exclude_none=True),
    }


def _validate_generic_source(source: ConnectorSource) -> dict[str, Any]:
    """Validate an RSS/web/REST source with no provider and return its collector snapshot.

    Provider-scope keys are rejected (they need a registered provider); the feed/page URL is
    required, REST needs ``items_path`` and the default HTTP(S) port. Pure; raises ``ValueError``.
    """
    if set(source.configuration or {}) & (COLLECTION_FILTER_FIELDS | {
        "feed_sections", *sorted(ALL_OPTION_NAMES - COLLECTION_FILTER_FIELDS), "youtube_channel_id", "arxiv_category", "huggingface_author", "bluesky_actor",
        "github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "telegram_chat_ids", "history_mode",
        "market_symbols", "market_currency", "market_exchange_timezone", "coingecko_assets", "weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics",
        "news_query", "news_site", "news_locale", "sec_cik", "sec_contact_email", "scholarly_query",
        "fred_series_id", "owm_latitude", "owm_longitude", "openaq_location_id", "firms_country",
        "google_mail_label_ids", "google_mail_history_days", "google_calendar_ids", "google_drive_ids",
        "fhir_base_url", "fhir_patient_id", "ha_base_url", "ha_entity_ids",
    }):
        raise ValueError("Provider scope requires a registered source provider")
    config = configuration(source)
    required_url = config.feed_url if source.type == "rss" else config.url
    if required_url is None:
        raise ValueError("Source connector URL is not configured")
    if source.type == "api" and not config.items_path:
        raise ValueError("REST connector requires items_path")
    if source.type == "api":
        parsed_url = urlsplit(str(required_url))
        expected_port = 443 if parsed_url.scheme == "https" else 80
        if parsed_url.port not in (None, expected_port):
            raise ValueError("REST connector URLs must use the default HTTP(S) port")
    return {
        "source_id": str(source.id),
        "source_generation": source.generation,
        "type": source.type,
        "timezone": config.timezone or DEFAULT_TIMEZONE,
        "url": str(required_url),
        "configuration": config.model_dump(mode="json", exclude_none=True),
    }


def health(source: ConnectorSource) -> dict[str, str]:
    """Report source lifecycle or whether its packaged connector configuration is ready."""
    if source.status != "active":
        return {"status": source.status, "connector": source.type}
    try:
        validate(source)
    except ValueError:
        return {"status": "misconfigured", "connector": source.type}
    return {"status": "ready", "connector": source.provider or source.type}


def sync(source: ConnectorSource, cursor: str | None) -> dict[str, Any]:
    """Build the validated n8n dispatch payload and current cursor state."""
    from modules.connectors.n8n import workflow_state

    state = validate(source)
    return state if is_native_provider(source.provider) else {**state, **workflow_state(cursor)}


def normalize(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider fields and timestamps into the bounded ingestion record shape."""
    from datetime import UTC, datetime

    raw_timestamp = str(record.get("observed_at") or "")
    timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")) if raw_timestamp else datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return {
        "provider_id": str(record.get("provider_id") or record.get("url") or "")[:512],
        "content": str(record.get("content") or "")[:1_000_000],
        "observed_at": timestamp.astimezone(UTC).isoformat(),
        "version": str(record["version"])[:255] if record.get("version") is not None else None,
        "metadata": dict(record.get("metadata") or {}),
    }
