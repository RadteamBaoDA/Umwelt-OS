from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from modules.connectors.backends import (
    FIXED_SCOPE_PROVIDERS,
    GENERIC_CATALOG_SOURCE_TYPES,
    PROVIDER_SCOPE_FIELDS,
    PROVIDER_SOURCE_TYPES,
    is_native_provider,
)
from modules.connectors.provider_specs import (
    FREE_PROVIDER_SPECS,
    FreeProviderSpec,
    QuotaWindowPolicy,
)


class CatalogEntry(BaseModel):
    """Describe source kind and readiness evidence separately from runtime acceptance.

    Manual imports use existing upload receipts and never enter scheduler dispatch. Code presence
    grants neither runtime acceptance nor contextual provider eligibility.
    """

    model_config = ConfigDict(frozen=True)

    provider_id: str
    kind: Literal["connector", "group", "preset", "manual_import"] = "connector"
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
    runtime_verified: bool = False
    eligibility: Literal["unknown", "open", "personal", "noncommercial", "review"] = "unknown"
    # Additive free-provider facts (see provider_specs). Facts and evidence are separate: none implies
    # runtime acceptance, and eligibility here is the catalog class, not a per-workspace grant.
    terms_url: str | None = None
    terms_checked_on: str | None = None
    attribution: str | None = None
    category: Literal["news", "economy", "hazards", "space", "security", "tech"] | None = None
    hosts: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()
    execution: Literal["supported", "experimental", "research_only"] | None = None
    default_interval_minutes: int | None = None
    key_fields: tuple[str, ...] = ()
    endpoint_probe: Literal["plan_documented", "unverified"] | None = None
    quota_policies: tuple[QuotaWindowPolicy, ...] = ()
    setup_guide: str | None = None
    # Optional sidecar the provider needs; None means the native base stack is enough.
    requires_service: Literal["n8n", "browser"] | None = None
    # Static, fixture-shaped illustrations for the setup guide. Never live data and never a quota or license claim.
    example_config: dict[str, Any] | None = None
    sample_output: dict[str, Any] | None = None

    @computed_field(return_type=bool)  # type: ignore[prop-decorator]  # pydantic computed_field over property
    @property
    def code_available(self) -> bool:
        """Report an adapter actually wired to collection, independently of credentials or runtime readiness.

        Native provider identity alone is insufficient: a source type may be
        registered before its scheduler adapter exists.
        """
        if self.kind == "manual_import":
            return True
        if self.provider_id in GENERIC_CATALOG_SOURCE_TYPES:
            return True
        if is_native_provider(self.provider_id):
            from modules.connectors.collection import provider_collection_available

            return provider_collection_available(self.provider_id)
        return False

    @computed_field(return_type=str | None)  # type: ignore[prop-decorator]  # pydantic computed_field over property
    @property
    def source_type(self) -> str | None:
        """Return the registered source type, including manual imports outside connector dispatch."""
        if self.kind == "manual_import":
            return "manual"
        return PROVIDER_SOURCE_TYPES.get(self.provider_id, GENERIC_CATALOG_SOURCE_TYPES.get(self.provider_id))


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
        requires_service="browser",
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
    CatalogEntry(
        provider_id="reddit", label="Reddit public subreddit posts", auth_methods=("oauth2",),
        scope_fields=("reddit_subreddit",),
        configuration_fields=("reddit_subreddit", "schedule_interval_minutes", "timezone"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 100, "response_bytes_per_trigger": 5_242_880},
        history_description="One page of up to 100 newest posts per trigger. Reddit's `after` fullname cursor continues the selected subreddit snapshot; no votes, comments, or private communities are collected.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="requires_credentials",
        availability_reason="Connect a separately configured Reddit app with the read-only `read` scope. Reddit does not support PKCE in this confidential-client flow; runtime behavior and provider limits are unverified.",
        license_status="unverified", evidence_status="official_reddit_oauth_read_api_runtime_unverified",
        hosts=("www.reddit.com", "oauth.reddit.com"), category="tech",
        endpoints=("/api/v1/authorize", "/api/v1/access_token", "/r/{subreddit}/new.json"),
    ),
    CatalogEntry(
        provider_id="bluesky", label="Bluesky public account feed", auth_methods=("none",),
        scope_fields=("bluesky_actor",), configuration_fields=("bluesky_actor", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 1, "records_per_trigger": 100, "response_bytes": 5_242_880},
        history_description="Latest public posts from one configured account, at most 100 per trigger; older posts require a provider cursor and are not backfilled yet.",
        collection_modes=("scheduled", "manual"), supports_history=False, supports_edit=False,
        supports_delete=False, availability="implemented", availability_reason="Public AppView reads only; the source requires no write permission or credential.",
        license_status="unverified", evidence_status="official_public_appview_api", eligibility="unknown",
        hosts=("public.api.bsky.app", "bsky.app"), category="tech",
        endpoints=("/xrpc/app.bsky.feed.getAuthorFeed",),
    ),
    CatalogEntry(
        provider_id="mastodon", label="Mastodon public posts", auth_methods=("none", "http_header"),
        scope_fields=("mastodon_instance_url", "mastodon_account", "mastodon_hashtag"),
        configuration_fields=("mastodon_instance_url", "mastodon_account", "mastodon_hashtag", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 2, "records_per_trigger": 40, "response_bytes_per_trigger": 5_242_880},
        history_description="One public-account or hashtag timeline page of up to 40 posts; a validated post ID continues the snapshot. Private and follower-only posts are excluded.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="implemented",
        availability_reason="Read-only public API calls to the explicitly selected HTTPS instance; an owner token may be supplied for instances that require authorization. Live behavior and instance limits are unverified.",
        license_status="unverified", evidence_status="official_mastodon_rest_api_runtime_unverified",
    ),
    CatalogEntry(
        provider_id="x", label="X public posts", auth_methods=("http_header",),
        scope_fields=("x_account", "x_query"),
        configuration_fields=("x_account", "x_query", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 2, "records_per_trigger": 100, "response_bytes_per_trigger": 5_242_880},
        history_description="One account-timeline or recent-search page (up to 100 posts) per trigger; provider pagination tokens continue the bounded snapshot. Coverage depends on the owner's X API entitlement.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=False,
        supports_delete=False, availability="requires_credentials",
        availability_reason="An owner-provided X app bearer token with read access is required; endpoint availability and page limits depend on the provider plan.",
        license_status="unverified", evidence_status="official_x_api_documented_runtime_unverified",
        endpoints=("/2/users/by/username/{username}", "/2/users/{id}/tweets", "/2/tweets/search/recent"),
    ),
    CatalogEntry(
        provider_id="google_mail", label="Gmail", auth_methods=("oauth2",),
        scope_fields=("google_mail_label_ids",),
        configuration_fields=("google_mail_label_ids", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 25, "records_per_trigger": 500, "response_bytes_per_trigger": 26_214_400},
        history_description="Initial snapshot covers selected labels for the prior 30 days; later runs replay Gmail history changes. Attachments are not downloaded.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A separate read-only Google connector grant is required.",
        evidence_status="official_gmail_readonly_api", hosts=("gmail.googleapis.com",),
        endpoints=("/gmail/v1/users/me/messages", "/gmail/v1/users/me/history"),
    ),
    CatalogEntry(
        provider_id="google_calendar", label="Google Calendar", auth_methods=("oauth2",),
        scope_fields=("google_calendar_ids",),
        configuration_fields=("google_calendar_ids", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 25, "records_per_trigger": 500, "response_bytes_per_trigger": 26_214_400},
        history_description="Selected calendars start at 30 days before through 90 days ahead; per-calendar sync tokens preserve recurring and cancelled events.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A separate read-only Google connector grant is required.",
        evidence_status="official_calendar_readonly_api", hosts=("www.googleapis.com",),
        endpoints=("/calendar/v3/calendars/{calendarId}/events",),
    ),
    CatalogEntry(
        provider_id="google_drive", label="Google Drive", auth_methods=("oauth2",),
        scope_fields=("google_drive_ids",),
        configuration_fields=("google_drive_ids", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 25, "records_per_trigger": 500, "response_bytes_per_trigger": 26_214_400},
        history_description="Snapshots selected files and follows Drive changes. Google Docs export as text; only existing parser-supported text formats are read.",
        collection_modes=("scheduled", "manual"), supports_history=True, supports_edit=True,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A separate read-only Google connector grant is required.",
        evidence_status="official_drive_readonly_api", hosts=("www.googleapis.com",),
        endpoints=("/drive/v3/files", "/drive/v3/changes"),
    ),
    CatalogEntry(
        provider_id="health", label="FHIR R4 observations", auth_methods=("http_header",),
        scope_fields=("fhir_base_url", "fhir_patient_id"),
        configuration_fields=("fhir_base_url", "fhir_patient_id", "schedule_interval_minutes"),
        quota_limits={"pages_per_trigger": 10, "records_per_trigger": 500, "response_bytes_per_page": 2_097_152},
        history_description="Patient-scoped Observation search for the prior 30 days; records retain source IDs, value, unit and measurement time. No write operations are exposed.",
        collection_modes=("scheduled", "manual"), supports_history=False, supports_edit=False,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A configured FHIR R4 endpoint, exact patient ID and owner-entered bearer token are required; each request stays on that endpoint origin and port.",
        evidence_status="fhir_r4_observation_read_only_adapter",
    ),
    CatalogEntry(
        provider_id="home_assistant", label="Home Assistant", auth_methods=("http_header",),
        scope_fields=("ha_base_url", "ha_entity_ids"),
        configuration_fields=("ha_base_url", "ha_entity_ids", "schedule_interval_minutes"),
        quota_limits={"requests_per_trigger": 25, "records_per_trigger": 500, "response_bytes_per_request": 2_097_152, "response_bytes_per_trigger": 10_485_760},
        history_description="Current-state snapshot of selected entities only, at most 25 GETs per durable receipt; a scope-bound cursor resumes up to 500 entities across receipts. No history backfill.",
        collection_modes=("scheduled", "manual"), supports_history=False, supports_edit=False,
        supports_delete=False, availability="requires_credentials",
        availability_reason="A configured Home Assistant endpoint, selected entity IDs and owner-entered bearer token are required; only read-only state endpoints are called.",
        evidence_status="home_assistant_state_snapshot_adapter",
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
            kind="manual_import" if provider_id in {"notes", "personal_finance"} else "connector",
            scope_fields=(), collection_modes=("manual_import",) if provider_id in {"notes", "personal_finance"} else (), supports_history=False,
            supports_edit=False, supports_delete=False, availability=availability,
            availability_reason=reason, history_description=None,
            evidence_status=evidence,
            requires_service=None,
        )
        for provider_id, label, auth_methods, availability, reason, evidence in (
            ("notes", "Notes (Markdown import)", ("none",), "available", "Markdown files use the existing owner-scoped upload and durable ingestion receipt; no server directory is read.", "manual_import"),
            ("personal_finance", "Personal finance (OFX/CSV import)", ("none",), "available", "Owner-supplied OFX/CSV is normalized through the existing manual import receipt; no bank connection or transaction execution is provided.", "manual_import"),
            ("notion", "Notion", ("oauth2",), "planned", "OAuth and collection capability remain open.", "unverified"),
            ("slack", "Slack", ("oauth2",), "planned", "OAuth and collection capability remain open.", "unverified"),
        )
    ),
)


_NEWS = {"title": "Example headline", "url": "https://example.org/article", "publisher": "Example Publisher",
         "published_at": "2026-01-01T08:00:00Z"}
SAMPLE_OUTPUTS: dict[str, dict[str, Any]] = {
    "bbc_world": _NEWS, "vnexpress_business": _NEWS, "tuoitre_news": _NEWS, "thanhnien_news": _NEWS, "dantri_news": _NEWS,
    "gdelt_economy": _NEWS,
    **dict.fromkeys((
        "aljazeera_world", "npr_news", "fed_press", "gdacs_rss", "lobsters", "us_travel_advisories",
        "noaa_tsunami", "jma_eqvol",
    ), _NEWS),
    **dict.fromkeys((
        "gdacs_events", "nws_alerts", "emsc_quakes", "launch_library", "wikipedia_mostread", "wikipedia_top_views", "bmkg_quakes", "satnogs_observations",
    ), _NEWS),
    "hn_top": {"title": "Example story", "url": "https://example.org/story", "by": "example_user", "score": 123,
               "published_at": "2026-01-01T08:00:00Z"},
    "world_bank": {"indicator": "NY.GDP.MKTP.CD", "country": "VN", "period": "2024", "value": 1.0e11},
    "frankfurter": {"base": "EUR", "quote": "USD", "rate": 1.0, "reference_date": "2026-01-01"},
    "ecb": {"base": "EUR", "quote": "USD", "rate": 1.0, "reference_date": "2026-01-01"},
    "binance": {"symbol": "BTCUSDT", "price": "100000.00", "observed_at": "2026-01-01T08:00:00Z"},
    "alternative_me": {"index": "fear_and_greed", "value": 50, "classification": "Neutral", "reference_date": "2026-01-01"},
    "usgs": {"magnitude": 4.5, "place": "Example region", "event_time": "2026-01-01T08:00:00Z"},
    "nasa_eonet": {"title": "Wildfire Example", "categories": "Wildfires", "event_time": "2026-01-01T08:00:00Z"},
    "cisa_kev": {"cveID": "CVE-2026-0001", "vendorProject": "Example", "product": "Example product",
                 "dateAdded": "2026-01-01", "dueDate": "2026-01-22", "knownRansomwareCampaignUse": "Unknown"},
    "coinpaprika": {"coin_id": "btc-bitcoin", "price_usd": 100000.0, "observed_at": "2026-01-01T08:00:00Z"},
    "google_news": {"title": "Example headline", "url": "https://news.google.com/rss/articles/EXAMPLE",
                    "publisher": "Reuters", "published_at": "2026-01-01T08:00:00Z"},
    "sec_edgar": {"accession": "0000320193-26-000001", "form": "8-K", "filing_date": "2026-01-01", "cik": "0000320193"},
    "fred": {"series_id": "DFF", "date": "2026-01-01", "value": 4.33},
    "finnhub_news": _NEWS, "newsapi": _NEWS,
    "openweathermap": {"metric": "temperature", "value": 28.4, "unit": "degC", "observed_at": "2026-01-01T08:00:00Z"},
    "openaq": {"metric": "pm25", "value": 18.0, "unit": "ug/m3", "observed_at": "2026-01-01T08:00:00Z"},
    "nasa_firms": {"latitude": 21.1, "longitude": 105.9, "frp_mw": 4.2, "acq": "2026-01-01T08:00:00+00:00"},
    "noaa_kp": {"time_tag": "2026-01-01T00:00:00+00:00", "kp": 2.33},
    "open_er_api": {"base": "USD", "quote": "VND", "rate": 26000.0, "date": "2026-01-01"},
    "oecd_cli": {"area": "USA", "period": "2026-01", "value": 100.0, "unit": "index"},
    "iss_position": {"latitude": -30.1, "longitude": 65.2, "altitude_km": 427.3, "observed_at": "2026-01-01T08:00:00Z"},
    "disease_sh": {"metric": "cases", "region": "Global", "value": 700000000},
    "ioda": {"location": "country/PG", "datasource": "bgp", "start": "2026-01-01T08:00:00Z", "score": 24448.5},
    "nvd": {"cveID": "CVE-2026-0001", "severity": "HIGH", "base_score": 7.5, "published": "2026-01-01T08:00:00+00:00"},
    "coingecko": {"coin_id": "bitcoin", "price_usd": 100000.0, "observed_at": "2026-01-01T08:00:00Z"},
    "openalex": {"title": "Example paper", "canonical_url": "https://openalex.org/W0000000000", "doi": "https://doi.org/10.0000/example",
                 "authors": ["Example Author"], "cited_by_count": 0, "published_at": "2026-01-01T00:00:00+00:00"},
}


# Scoped free presets: owner scope is validated server-side and the URL is never user-supplied.
_SCOPED_EXAMPLES: dict[str, dict[str, Any]] = {
    "google_news": {"news_query": "Vietnam economy", "news_site": "reuters.com", "news_locale": "en-US"},
    "sec_edgar": {"sec_cik": "0000320193", "sec_contact_email": "owner@example.com"},
    "openalex": {"scholarly_query": "large language models"},
    "fred": {"fred_series_id": "DFF"},
    "openweathermap": {"owm_latitude": 21.0285, "owm_longitude": 105.8542},
    "openaq": {"openaq_location_id": 8118},
    "nasa_firms": {"firms_country": "VNM"},
}


def _with_spec(entry: CatalogEntry, spec: FreeProviderSpec) -> CatalogEntry:
    """Overlay free-provider facts on an entry without touching its availability or scope fields."""
    fixed = spec.id in FIXED_SCOPE_PROVIDERS
    scoped = _SCOPED_EXAMPLES.get(spec.id)
    return entry.model_copy(update={
        "example_config": {
            "timeout_seconds": 30, "timezone": "Asia/Ho_Chi_Minh",
            "schedule_interval_minutes": spec.default_interval_minutes, "history_mode": "returned_snapshot",
            **(scoped or {}),
        } if fixed or scoped else None,
        "sample_output": SAMPLE_OUTPUTS.get(spec.id) if fixed or scoped else None,
        "eligibility": spec.eligibility, "terms_url": spec.terms_url, "terms_checked_on": spec.checked_on,
        "attribution": spec.attribution, "category": spec.category, "hosts": spec.hosts, "endpoints": spec.endpoints,
        "execution": spec.execution, "default_interval_minutes": spec.default_interval_minutes,
        "key_fields": ("api_key",) if spec.key_required else (), "endpoint_probe": spec.endpoint_probe,
        "quota_policies": spec.quota, "setup_guide": (
            f"Opt in per source, declare deployment use and acknowledge the terms ({spec.terms_url})."
            + (" Provide your own free API key; no key is created or shared automatically." if spec.key_required else "")
            + (" An operator must record a terms review before this provider can run." if spec.eligibility == "review" else "")
        ),
    })


def _free_entry(spec: FreeProviderSpec) -> CatalogEntry:
    # Planned until the adapter is registered (spec.code_implemented): planned entries must never pass
    # the "available/implemented/requires_credentials" provider-scope gates.
    ready = spec.code_implemented
    scope = tuple(sorted(PROVIDER_SCOPE_FIELDS.get(spec.id, ()) if spec.id not in FIXED_SCOPE_PROVIDERS else ()))
    return _with_spec(CatalogEntry(
        provider_id=spec.id, kind="preset", label=spec.label, auth_methods=("api_key",) if spec.key_required else ("none",),
        scope_fields=scope, configuration_fields=(*scope, "schedule_interval_minutes"),
        collection_modes=("scheduled", "manual") if ready else (),
        supports_history=False, supports_edit=False, supports_delete=False,
        availability="implemented" if ready else "planned",
        availability_reason=(
            "Adapter registered for the gated shared executor; live endpoint behavior is not runtime-verified."
            if ready else "Catalog preset only: no collection adapter is registered or runtime-verified yet."),
        license_status="unverified", evidence_status="plan_documented_endpoint_unverified_runtime",
    ), spec)


_SPECS = {spec.id: spec for spec in FREE_PROVIDER_SPECS}
_ENTRIES = tuple(_with_spec(entry, _SPECS[entry.provider_id]) if entry.provider_id in _SPECS else entry for entry in _ENTRIES)
_ENTRIES += tuple(_free_entry(spec) for spec in FREE_PROVIDER_SPECS if spec.id not in {e.provider_id for e in _ENTRIES})
_WORKSPACE_APP_FACTS = {
    "notion": {
        "auth_methods": ("http_header",),
        "scope_fields": ("notion_page_ids",),
        "configuration_fields": ("notion_page_ids", "schedule_interval_minutes", "timezone"),
        "collection_modes": ("scheduled", "manual"),
        "supports_history": True,
        "availability": "requires_credentials",
        "availability_reason": "Selected Notion pages require an internal integration token; runtime behavior is unverified.",
        "history_description": "Selected page text snapshot; file contents, attachments, databases, and workspace-wide search are excluded.",
        "evidence_status": "official_api_version_pinned_runtime_unverified",
    },
    "slack": {
        "auth_methods": ("http_header",),
        "scope_fields": ("slack_channel_ids",),
        "configuration_fields": ("slack_channel_ids", "schedule_interval_minutes", "timezone"),
        "collection_modes": ("scheduled", "manual"),
        "supports_history": True,
        "availability": "requires_credentials",
        "availability_reason": "Selected public/private channels require a bot token and bot membership; runtime behavior is unverified.",
        "history_description": "Top-level messages from the selected channels, bounded to the last 30 days by default; DMs and thread replies are excluded.",
        "evidence_status": "official_web_api_documented_runtime_unverified",
    },
}
_ENTRIES = tuple(
    entry.model_copy(update=_WORKSPACE_APP_FACTS[entry.provider_id])
    if entry.provider_id in _WORKSPACE_APP_FACTS else entry
    for entry in _ENTRIES
)

_ENTRIES = tuple(
    entry.model_copy(update={"kind": "preset"})
    if entry.provider_id in _SPECS and entry.kind == "connector" else entry
    for entry in _ENTRIES
)
_ENTRIES = tuple(
    entry.model_copy(update={"collection_modes": ("manual_import",)})
    if entry.kind == "manual_import" else entry
    for entry in _ENTRIES
)


def list_catalog() -> tuple[CatalogEntry, ...]:
    """Return the immutable provider catalog in its declared display order."""
    return _ENTRIES


def get_catalog_entry(provider_id: str) -> CatalogEntry | None:
    """Return the catalog record for an exact provider ID, or None if absent."""
    return next((entry for entry in _ENTRIES if entry.provider_id == provider_id), None)
