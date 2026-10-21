"""Connector-owned capability identities, separate from runtime admission and trust."""

from types import MappingProxyType
from typing import Any, Literal, TypeGuard

CollectionBackend = Literal["native", "n8n"]

PROVIDER_SOURCE_TYPES = MappingProxyType({
    "youtube": "rss", "arxiv": "rss", "huggingface": "api",
    "github_releases": "api", "github": "api", "telegram": "api",
    "alpha_vantage": "api", "open_meteo": "api",
    # P2/P3 fixed-endpoint providers: no owner-supplied scope, one catalog request per run.
    "bbc_world": "rss", "vnexpress_business": "rss", "dantri_news": "rss", "thanhnien_news": "rss", "tuoitre_news": "rss", "hn_top": "api", "gdelt_economy": "api",
    "world_bank": "api", "frankfurter": "api", "ecb": "api", "binance": "api",
    "alternative_me": "api", "usgs": "api", "coinpaprika": "api", "nasa_eonet": "api", "cisa_kev": "api",
    "coingecko": "api",  # fixed endpoint; owner-supplied API key lives in connector_rest_credentials
    "google_news": "rss",  # scoped free preset: URL is built server-side from validated query/site/locale
    "sec_edgar": "api",  # scoped free preset: URL is built server-side from a validated 10-digit CIK
    "openalex": "api",  # scoped free preset: URL is built server-side from a validated scholarly_query
    **dict.fromkeys((  # built-in RSS/Atom feeds (providers/news.py FEED_URLS)
        "aljazeera_world", "npr_news", "fed_press", "gdacs_rss", "lobsters", "us_travel_advisories",
        "noaa_tsunami", "jma_eqvol",
    ), "rss"),
    **dict.fromkeys((  # built-in JSON feeds (providers/json_feed.py)
        "gdacs_events", "nws_alerts", "emsc_quakes", "launch_library", "wikipedia_mostread", "wikipedia_top_views", "bmkg_quakes", "satnogs_observations",
    ), "api"),
    **dict.fromkeys((  # keyed free-tier presets (providers/keyed.py); the owner key lives in connector_rest_credentials
        "finnhub_news", "newsapi", "fred", "openweathermap", "openaq", "nasa_firms",
    ), "api"),
    **dict.fromkeys((  # bespoke keyless mappers
        "noaa_kp", "open_er_api", "oecd_cli", "iss_position", "disease_sh", "ioda", "nvd",
    ), "api"),
})
FIXED_SCOPE_PROVIDERS = frozenset(PROVIDER_SOURCE_TYPES) - frozenset({
    "youtube", "arxiv", "huggingface", "github_releases", "github", "telegram", "alpha_vantage", "open_meteo", "google_news", "sec_edgar",
    "openalex", "fred", "openweathermap", "openaq", "nasa_firms",
})
# Free presets collected only by the gated shared executor (fixed-endpoint presets plus the scoped Google News preset).
GATED_EXECUTOR_PROVIDERS = FIXED_SCOPE_PROVIDERS | {
    "google_news", "sec_edgar", "openalex", "fred", "openweathermap", "openaq", "nasa_firms",
}
PROVIDER_SCOPE_FIELDS = MappingProxyType({
    "youtube": frozenset({"youtube_channel_id"}), "arxiv": frozenset({"arxiv_category"}),
    "huggingface": frozenset({"huggingface_author"}),
    "github_releases": frozenset({"github_owner", "github_repository"}),
    "github": frozenset({
        "github_owner", "github_repository", "include_issues", "include_pulls", "include_commits",
        "include_releases", "github_history_days",
    }),
    "telegram": frozenset({"telegram_chat_ids"}),
    "alpha_vantage": frozenset({"market_symbols", "market_currency", "market_exchange_timezone"}),
    "open_meteo": frozenset({"weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics"}),
    "google_news": frozenset({"news_query", "news_site", "news_locale"}),
    "sec_edgar": frozenset({"sec_cik", "sec_contact_email"}),
    "openalex": frozenset({"scholarly_query"}),
    "fred": frozenset({"fred_series_id"}),
    "openweathermap": frozenset({"owm_latitude", "owm_longitude"}),
    "openaq": frozenset({"openaq_location_id"}),
    "nasa_firms": frozenset({"firms_country"}),
    **dict.fromkeys(FIXED_SCOPE_PROVIDERS, frozenset[str]()),
})
NATIVE_PROVIDERS = frozenset(PROVIDER_SOURCE_TYPES)
GENERIC_CATALOG_SOURCE_TYPES = MappingProxyType({
    "rss": "rss", "web": "web", "rest": "api", "mcp": "mcp",
})
GENERIC_SOURCE_TYPES = frozenset(GENERIC_CATALOG_SOURCE_TYPES.values())


def is_native_provider(provider: str | None) -> TypeGuard[str]:
    """Identify exact registered providers eligible for typed provider provenance.

    Execution capability for a generic source never grants this identity; stored
    source configuration, collection fences and envelope checks remain required.
    """
    return provider in NATIVE_PROVIDERS


def native_dispatch_supported(source_type: str, provider: str | None) -> bool:
    """Report backend capability for a stored source type/provider pair.

    Reject unknown or mismatched providers. Generic capabilities describe the
    execution contract, not active native scheduling, runtime verification,
    credentials, eligibility or permission to submit trusted provider metadata.
    Callers must derive provider from their persisted source, not an override.
    """
    if provider is None:
        return source_type in GENERIC_SOURCE_TYPES
    return is_native_provider(provider) and PROVIDER_SOURCE_TYPES[provider] == source_type


# C4: backend/template revision protocol. Bump the template revision whenever a packaged n8n
# workflow changes its fences; rows installed with an older template fail closed until upgraded.
CURRENT_TEMPLATE_REVISION = 1
TRANSITION_PHASES = ("idle", "draining", "deactivating_old", "activating_new", "reconciliation_required")


def backend_admits(row: Any) -> bool:
    """Prove a provisioning row may admit collection for its own backend.

    Only an idle row whose applied backend revision equals the current one admits work. n8n
    additionally needs a confirmed workflow and the current template; native needs neither.
    """
    if (
        row.transition_phase != "idle"
        or row.applied_backend_revision != row.backend_revision
        or row.execution_backend not in ("native", "n8n")
    ):
        return False
    if row.execution_backend == "native":
        return True
    return bool(row.workflow_id) and row.applied_template_revision >= CURRENT_TEMPLATE_REVISION
