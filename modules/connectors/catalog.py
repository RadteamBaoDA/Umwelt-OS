from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CatalogEntry(BaseModel):
    """Describe a provider's supported configuration, scope, and operations."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    label: str
    auth_methods: tuple[str, ...]
    scope_fields: tuple[str, ...]
    configuration_fields: tuple[str, ...] = ()
    quota_limits: dict[str, int] = Field(default_factory=dict)
    history_description: str | None = None
    collection_modes: tuple[str, ...]
    supports_history: bool
    supports_edit: bool
    supports_delete: bool
    availability: Literal["implemented", "requires_credentials", "unsupported_operation", "planned", "unavailable", "available"]
    unavailable_reason: str | None = None
    unavailable_operations: tuple[str, ...] = ()
    availability_reason: str | None = None
    license_status: str | None = None
    evidence_status: str | None = None


_ENTRIES = (
    CatalogEntry(
        provider_id="rss",
        label="RSS / Atom",
        auth_methods=("none",),
        scope_fields=("feed_url",),
        configuration_fields=("feed_url", "timezone", "schedule_interval_minutes"),
        quota_limits={"max_pages": 10, "max_feed_bytes": 26_214_400, "max_records": 500, "max_batch_bytes": 10_485_760},
        history_description="Available feed entries within the current bounded feed response.",
        collection_modes=("scheduled", "manual"),
        supports_history=True,
        supports_edit=False,
        supports_delete=False,
        availability="available",
    ),
    CatalogEntry(
        provider_id="web",
        label="Web page",
        auth_methods=("none",),
        scope_fields=("url", "max_depth", "max_pages", "timeout_seconds", "js_render"),
        configuration_fields=("url", "max_pages", "max_depth", "timeout_seconds", "js_render", "timezone", "schedule_interval_minutes"),
        quota_limits={"max_pages": 10, "max_depth": 2, "timeout_seconds": 60, "max_job_bytes": 26_214_400, "max_records": 500, "max_batch_bytes": 10_485_760},
        history_description="Pages discovered within the configured page and depth limits.",
        collection_modes=("scheduled", "manual"),
        supports_history=True,
        supports_edit=False,
        supports_delete=False,
        availability="available",
    ),
    CatalogEntry(
        provider_id="rest",
        label="REST API",
        auth_methods=("none", "http_header"),
        scope_fields=("url", "items_path", "id_field", "title_field", "content_field"),
        configuration_fields=("url", "items_path", "id_field", "title_field", "content_field", "updated_field", "timezone", "schedule_interval_minutes"),
        quota_limits={"max_pages": 10, "max_records": 500, "max_batch_bytes": 10_485_760},
        history_description="Records returned by the current API response and pagination window.",
        collection_modes=("scheduled", "manual"),
        supports_history=True,
        supports_edit=False,
        supports_delete=False,
        availability="available",
        unavailable_operations=("automatic_recovery_after_lost_n8n_credential_create_id",),
    ),
    CatalogEntry(
        provider_id="mcp",
        label="MCP server",
        auth_methods=("none", "bearer"),
        scope_fields=("connection_id", "calls"),
        configuration_fields=("connection_id", "calls", "schedule_interval_minutes", "timezone"),
        quota_limits={"max_calls": 10, "max_records": 500, "max_response_bytes": 256_000},
        history_description=(
            "One request per allowlisted tool or resource per run; provider pagination is not followed "
            "and there is no backfill. Unchanged items repeat as observations."
        ),
        collection_modes=("scheduled", "manual"),
        supports_history=False,
        supports_edit=True,
        supports_delete=False,
        availability="available",
        unavailable_operations=(),
    ),
    CatalogEntry(
        provider_id="github",
        label="GitHub",
        auth_methods=("oauth2",),
        scope_fields=("github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "github_history_days"),
        configuration_fields=("github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "github_history_days", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 100, "response_bytes_per_resource": 2_097_152},
        history_description="One bounded page per trigger across selected resources; completed sweeps remain limited by the configured history horizon and page/object ceilings.",
        collection_modes=("scheduled", "manual"),
        supports_history=True,
        supports_edit=False,
        supports_delete=False,
        availability="requires_credentials",
        availability_reason="A GitHub App must be installed for the owner and grant read access to the selected repository resources.",
        evidence_status="official_github_app_user_oauth",
    ),
    *(
        CatalogEntry(
            provider_id=provider,
            label=label,
            auth_methods=("oauth2",),
            scope_fields=(),
            collection_modes=(),
            supports_history=False,
            supports_edit=False,
            supports_delete=False,
            availability="unavailable",
            unavailable_reason="No provider adapter consumes this authorization yet.",
        )
        for provider, label in (
            ("google_mail", "Gmail"),
            ("google_calendar", "Google Calendar"),
            ("google_drive", "Google Drive"),
        )
    ),
    CatalogEntry(
        provider_id="youtube", label="YouTube feeds", auth_methods=("none",),
        scope_fields=("youtube_channel_id", "history_mode"),
        configuration_fields=("youtube_channel_id", "history_mode", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 500},
        history_description="Entries returned by the current YouTube channel feed.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="implemented", evidence_status="official_feed",
    ),
    CatalogEntry(
        provider_id="arxiv", label="arXiv", auth_methods=("none",),
        scope_fields=("arxiv_category", "history_mode"),
        configuration_fields=("arxiv_category", "history_mode", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 500},
        history_description="Metadata entries returned by the current category feed.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="implemented", evidence_status="official_feed",
    ),
    CatalogEntry(
        provider_id="huggingface", label="Hugging Face models", auth_methods=("none",),
        scope_fields=("huggingface_author", "history_mode"),
        configuration_fields=("huggingface_author", "history_mode", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 100},
        history_description="Public model metadata in one bounded author snapshot.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="implemented", evidence_status="official_api",
    ),
    CatalogEntry(
        provider_id="github_releases", label="GitHub Releases", auth_methods=("none",),
        scope_fields=("github_owner", "github_repository", "history_mode"),
        configuration_fields=("github_owner", "github_repository", "history_mode", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 5, "records_per_trigger": 500},
        history_description="Public releases from the five-page snapshot; older edits may not be detected.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="implemented", evidence_status="official_api",
    ),
    CatalogEntry(
        provider_id="telegram", label="Telegram channels", auth_methods=("telegram_bot_token",),
        scope_fields=("telegram_chat_ids", "history_mode"),
        configuration_fields=("telegram_chat_ids", "history_mode", "timezone", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 5, "updates_per_trigger": 500},
        history_description="Authorized channel updates still available to the bot; edits are captured when received.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A bot token and administrator access to every configured channel must be validated.",
        evidence_status="official_bot_api",
    ),
    CatalogEntry(
        provider_id="alpha_vantage", label="Alpha Vantage daily equities", auth_methods=("api_key",),
        scope_fields=("market_symbols", "market_currency", "market_exchange_timezone"),
        configuration_fields=("market_symbols", "market_currency", "market_exchange_timezone", "schedule_interval_minutes"),
        quota_limits={"local_requests_per_trigger": 5, "local_requests_per_credential_per_utc_day": 25, "records_per_trigger": 25},
        history_description="At most one current daily OHLCV point per configured symbol per trigger; revisions accumulate in PostgreSQL.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="requires_credentials",
        availability_reason="API key is owner-encrypted; exact free/premium exchange entitlements, source currency/timezone metadata, terms, and activation must be confirmed.",
        license_status="unverified", evidence_status="official_schema_terms_pdf_uninspected",
    ),
    CatalogEntry(
        provider_id="open_meteo", label="Open-Meteo forecast", auth_methods=("none",),
        scope_fields=("weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics"),
        configuration_fields=("weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 288},
        history_description="Up to three days of selected hourly forecast variables with returned units and configured timezone.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="implemented",
        availability_reason="Non-commercial use and attribution are required under the documented free terms; commercial deployments require an appropriate subscription.",
        license_status="restricted_noncommercial", evidence_status="official_api_terms_and_schema_reviewed",
    ),
    *(
        CatalogEntry(
            provider_id=provider_id, label=label, auth_methods=auth_methods,
            scope_fields=(), collection_modes=(), supports_history=False,
            supports_edit=False, supports_delete=False, availability=availability,
            availability_reason=reason, history_description=None,
            evidence_status=evidence,
        )
        for provider_id, label, auth_methods, availability, reason, evidence in (
            ("google_news", "Google News feeds", ("none",), "planned", "Official feed construction is not established; use configured RSS only when an owner supplies a feed URL.", "unverified"),
            ("reddit", "Reddit", ("oauth2",), "planned", "Provider OAuth, endpoint, licensing, and quota gates remain open.", "unverified"),
            ("hacker_news", "Hacker News", ("none",), "planned", "A scoped adapter and evidence review remain open.", "unverified"),
            ("mastodon", "Mastodon", ("oauth2",), "planned", "Instance, authorization, licensing, and quota gates remain open.", "unverified"),
            ("bluesky", "Bluesky", ("oauth2",), "planned", "Authorization, licensing, and quota gates remain open.", "unverified"),
            ("x", "X", ("oauth2",), "planned", "Authorized API/provider and quota evidence remain open.", "unverified"),
            ("vietnamese_press", "Permitted Vietnamese press", ("none",), "planned", "Per-source permission and collection evidence are required.", "unverified"),
            ("gdelt_government", "GDELT / government", ("none",), "planned", "Source-specific adapters and license evidence remain open.", "unverified"),
            ("finance", "Finance", ("none",), "planned", "Only Alpha Vantage daily equities have an adapter; crypto, commodities, macro/government and composite series remain unavailable pending named schemas, data terms, entitlement, and scope.", "unverified"),
            ("weather_disaster_climate", "Weather / disaster / climate", ("none",), "planned", "Open-Meteo forecast is implemented; disaster/climate observations remain unavailable pending named authorized sources and their schemas/terms.", "unverified"),
            ("cyber_cve", "Cyber / CVE", ("none",), "planned", "Source scope and evidence review remain open.", "unverified"),
            ("map_osint", "Map / OSINT", ("none",), "planned", "Source scope and evidence review remain open.", "unverified"),
            ("browser", "Browser collection", ("none",), "unsupported_operation", "Browser execution remains an isolated capability gate.", "unverified"),
            ("notes", "Notes", ("none",), "planned", "No owner-facing native notes adapter is registered.", "unverified"),
            ("health", "Health", ("oauth2",), "planned", "No health provider adapter is registered.", "unverified"),
            ("personal_finance", "Personal finance", ("oauth2",), "planned", "No personal finance provider adapter is registered.", "unverified"),
            ("iot", "IoT", ("none",), "planned", "No IoT provider adapter is registered.", "unverified"),
            ("notion", "Notion", ("oauth2",), "planned", "OAuth and collection capability remain open.", "unverified"),
            ("slack", "Slack", ("oauth2",), "planned", "OAuth and collection capability remain open.", "unverified"),
            ("home_assistant", "Home Assistant", ("http_header",), "planned", "Local trust boundary and supported scope remain open.", "unverified"),
            ("mcp", "MCP", ("none",), "unsupported_operation", "MCP client/server capabilities are owned by the tools module.", "unverified"),
        )
    ),
)


def list_catalog() -> tuple[CatalogEntry, ...]:
    """Return the immutable provider catalog in its declared display order."""
    return _ENTRIES


def get_catalog_entry(provider_id: str) -> CatalogEntry | None:
    """Return the catalog record for an exact provider ID, or None if absent."""
    return next((entry for entry in _ENTRIES if entry.provider_id == provider_id), None)
