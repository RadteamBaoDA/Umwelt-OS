from typing import Any
from urllib.parse import urlsplit

from modules.connectors.backends import (
    GENERIC_SOURCE_TYPES,
    NATIVE_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
    PROVIDER_SOURCE_TYPES,
    is_native_provider,
)
from modules.connectors.public import DEFAULT_TIMEZONE, ConnectorConfig
from modules.sources.schemas import ConnectorSource

SUPPORTED_TYPES = GENERIC_SOURCE_TYPES - {"mcp"}


def configuration(source: ConnectorSource) -> ConnectorConfig:
    """Validate settings and apply the 30-second default only to native sources.

    Generic connectors retain their historical 60-second default. Native
    sources with an omitted stored timeout use the provider's 30-second bound.
    """
    raw_configuration = dict(source.configuration or {})
    if source.provider == "alpha_vantage":
        raw_configuration.setdefault("schedule_interval_minutes", 1440)
    config = ConnectorConfig.model_validate(raw_configuration)
    if is_native_provider(source.provider) and "timeout_seconds" not in (source.configuration or {}):
        config = config.model_copy(update={"timeout_seconds": 30})
    return config


def validate(source: ConnectorSource) -> dict[str, Any]:
    """Validate active packaged connector settings and return a collector snapshot."""
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
        if source.provider not in NATIVE_PROVIDERS:
            raise ValueError("Provider is not registered")
        if source.type != PROVIDER_SOURCE_TYPES[source.provider]:
            raise ValueError("Source type does not match the registered provider")
        scope_fields = PROVIDER_SCOPE_FIELDS[source.provider]
        common = {"timezone", "schedule_interval_minutes", "timeout_seconds"}
        expected_history = "pending_updates" if source.provider == "telegram" else "returned_snapshot"
        raw_keys = set(source.configuration or {})
        if raw_keys - scope_fields - common - {"history_mode"}:
            raise ValueError("Provider configuration contains unsupported fields")
        required_scope_fields = scope_fields - ({"github_history_days"} if source.provider == "github" else set())
        if raw_keys & required_scope_fields != required_scope_fields or source.configuration.get("history_mode") != expected_history:
            raise ValueError("Provider scope and history mode are required")
        config = configuration(source)
        if config.timeout_seconds > 30:
            raise ValueError("Native provider request timeout cannot exceed 30 seconds")
        if source.provider == "github_releases" and (not config.github_owner or not config.github_repository):
            raise ValueError("GitHub owner and repository are required")
        if source.provider == "alpha_vantage" and (
            not config.market_symbols or not config.market_currency or not config.market_exchange_timezone
        ):
            raise ValueError("Alpha Vantage symbols, currency, and exchange timezone are required")
        if source.provider == "open_meteo" and (
            config.weather_latitude is None or config.weather_longitude is None
            or not config.weather_timezone or not config.weather_metrics
        ):
            raise ValueError("Open-Meteo coordinates, timezone, and metric scope are required")
        if source.provider == "github":
            from modules.connectors.github.schemas import project_github_source_config

            project_github_source_config(config.model_dump(exclude_none=True))
        return {
            "source_id": str(source.id),
            "source_generation": source.generation,
            "type": source.type,
            "provider": source.provider,
            "timezone": config.timezone or DEFAULT_TIMEZONE,
            "configuration": config.model_dump(mode="json", exclude_none=True),
        }
    if set(source.configuration or {}) & {
        "youtube_channel_id", "arxiv_category", "huggingface_author",
        "github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "telegram_chat_ids", "history_mode",
        "market_symbols", "market_currency", "market_exchange_timezone", "weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics",
        "news_query", "news_site", "news_locale",
    }:
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
    timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")) if raw_timestamp else datetime.now(UTC)  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return {
        "provider_id": str(record.get("provider_id") or record.get("url") or "")[:512],
        "content": str(record.get("content") or "")[:1_000_000],
        "observed_at": timestamp.astimezone(UTC).isoformat(),
        "version": str(record["version"])[:255] if record.get("version") is not None else None,
        "metadata": dict(record.get("metadata") or {}),
    }
