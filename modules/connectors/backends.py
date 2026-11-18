"""Connector-owned capability identities, separate from runtime admission and trust."""

from types import MappingProxyType
from typing import Any, Literal, TypeGuard

CollectionBackend = Literal["native", "n8n"]

# These API presets reuse the revisioned REST credential slot without an owner-configurable header.
NATIVE_SIGNED_MEDIA_PROVIDERS = frozenset({"youtube_api", "podcast_index", "igdb_games"})
YOUTUBE_TARGET_FIELDS = frozenset(
    {"youtube_channel_id", "youtube_channels", "youtube_handles", "youtube_playlist_ids"}
)
YOUTUBE_SCOPE_FIELDS = YOUTUBE_TARGET_FIELDS | {"youtube_filter"}
PODCAST_INDEX_SCOPE_FIELDS = frozenset({"podcast_index_query"})
LISTENBRAINZ_SCOPE_FIELDS = frozenset(
    {"listenbrainz_username"}
)
STEAM_SCOPE_FIELDS = frozenset({"steam_mode", "steam_country", "steam_language", "steam_appids"})
IGDB_SCOPE_FIELDS = frozenset({"igdb_game_types", "igdb_region", "igdb_platforms"})

PROVIDER_SOURCE_TYPES = MappingProxyType(
    {
        "youtube": "rss",
        **dict.fromkeys(NATIVE_SIGNED_MEDIA_PROVIDERS, "api"),
        "listenbrainz_releases": "api",
        "steam_releases": "api",
        "podcast_rss": "rss",
        "ann_news": "rss",
        "letterboxd_rss": "rss",
        "arxiv": "rss",
        "huggingface": "api",
        "bluesky": "api",
        "reddit": "api",
        "mastodon": "api",
        "x": "api",
        "github_releases": "api",
        "github": "api",
        "telegram": "api",
        "alpha_vantage": "api",
        "open_meteo": "api",
        # P2/P3 fixed-endpoint providers: no owner-supplied scope, one catalog request per run.
        "bbc_world": "rss",
        "vnexpress_business": "rss",
        "dantri_news": "rss",
        "thanhnien_news": "rss",
        "tuoitre_news": "rss",
        "hn_top": "api",
        "gdelt_economy": "api",
        "world_bank": "api",
        "frankfurter": "api",
        "ecb": "api",
        "binance": "api",
        "vn_stock": "api",
        "vietnamworks_jobs": "api",
        "itviec_jobs": "api",
        "alternative_me": "api",
        "usgs": "api",
        "coinpaprika": "api",
        "nasa_eonet": "api",
        "cisa_kev": "api",
        "coingecko": "api",  # owner-selected assets; Demo key lives in connector_rest_credentials
        "google_news": "rss",  # scoped free preset: URL is built server-side from validated query/site/locale
        "google_mail": "email",
        "google_calendar": "calendar",
        "google_drive": "api",
        "health": "api",
        "home_assistant": "api",
        "notion": "api",
        "slack": "api",
        "sec_edgar": "api",  # scoped free preset: URL is built server-side from a validated 10-digit CIK
        "tvmaze_schedule": "api",
        "livechart_episodes": "rss",
        "anilist_anime": "api",
        "anilist_manga": "api",
        "openlibrary_books": "api",
        "gutendex_books": "api",
        "hardcover_books": "api",
        "mangaupdates_releases": "api",
        "mangadex_releases": "api",
        "apple_books_rss": "api",
        "wikidata_movies": "api",
        "itunes_movies": "api",
        "openalex": "api",  # scoped free preset: URL is built server-side from a validated scholarly_query
        **dict.fromkeys(
            (  # built-in RSS/Atom feeds (providers/news.py FEED_URLS)
                "aljazeera_world",
                "npr_news",
                "fed_press",
                "gdacs_rss",
                "lobsters",
                "github_trending",
                "wwr_jobs",
                "us_travel_advisories",
                "noaa_tsunami",
                "jma_eqvol",
            ),
            "rss",
        ),
        **dict.fromkeys(
            (  # built-in JSON feeds (providers/json_feed.py)
                "gdacs_events",
                "nws_alerts",
                "emsc_quakes",
                "launch_library",
                "wikipedia_mostread",
                "wikipedia_top_views",
                "bmkg_quakes",
                "satnogs_observations",
            ),
            "api",
        ),
        **dict.fromkeys(
            (  # keyed free-tier presets (providers/keyed.py); the owner key lives in connector_rest_credentials
                "tmdb_movies",
                "tmdb_tv",
                "trakt_media",
                "omdb_lookup",
                "mal_anime",
                "mal_manga",
                "google_books",
                "finnhub_news",
                "newsapi",
                "fred",
                "openweathermap",
                "openaq",
                "nasa_firms",
            ),
            "api",
        ),
        **dict.fromkeys(
            (  # bespoke keyless mappers
                "noaa_kp",
                "open_er_api",
                "oecd_cli",
                "iss_position",
                "disease_sh",
                "ioda",
                "nvd",
            ),
            "api",
        ),
    }
)
FIXED_SCOPE_PROVIDERS = frozenset(PROVIDER_SOURCE_TYPES) - frozenset(
    {
        "youtube",
        *NATIVE_SIGNED_MEDIA_PROVIDERS,
        "listenbrainz_releases",
        "steam_releases",
        "arxiv",
        "huggingface",
        "bluesky",
        "reddit",
        "mastodon",
        "x",
        "github_releases",
        "github",
        "telegram",
        "alpha_vantage",
        "open_meteo",
        "google_news",
        "sec_edgar",
        "google_mail",
        "google_calendar",
        "google_drive",
        "health",
        "home_assistant",
        "notion",
        "slack",
        "openalex",
        "fred",
        "openweathermap",
        "openaq",
        "nasa_firms",
        "coingecko",
        "anilist_anime",
        "anilist_manga",
        "openlibrary_books",
        "gutendex_books",
        "ann_news",
        "mangaupdates_releases",
        "mangadex_releases",
        "apple_books_rss",
        "podcast_rss",
        "wikidata_movies",
        "itunes_movies",
        "letterboxd_rss",
        "omdb_lookup",
        "mal_anime",
        "mal_manga",
        "google_books",
        "hardcover_books",
    }
)
# Free presets collected only by the gated shared executor (fixed-endpoint presets plus the scoped Google News preset).
GATED_EXECUTOR_PROVIDERS = FIXED_SCOPE_PROVIDERS | NATIVE_SIGNED_MEDIA_PROVIDERS | {
    "listenbrainz_releases",
    "steam_releases",
    "bluesky",
    "reddit",
    "mastodon",
    "x",
    "google_news",
    "google_mail",
    "google_calendar",
    "google_drive",
    "health",
    "home_assistant",
    "notion",
    "slack",
    "sec_edgar",
    "openalex",
    "fred",
    "openweathermap",
    "openaq",
    "nasa_firms",
    "coingecko",
    "anilist_anime",
    "anilist_manga",
    "openlibrary_books",
    "gutendex_books",
    "ann_news",
    "mangaupdates_releases",
    "mangadex_releases",
    "apple_books_rss",
    "podcast_rss",
    "wikidata_movies",
    "itunes_movies",
    "letterboxd_rss",
    "mal_anime",
    "mal_manga",
    "google_books",
    "hardcover_books",
}
PROVIDER_SCOPE_FIELDS = MappingProxyType(
    {
        **dict.fromkeys(("youtube", "youtube_api"), YOUTUBE_SCOPE_FIELDS),
        "podcast_index": PODCAST_INDEX_SCOPE_FIELDS,
        "listenbrainz_releases": frozenset({
            "listenbrainz_username",
        }),
        "steam_releases": STEAM_SCOPE_FIELDS,
        "igdb_games": IGDB_SCOPE_FIELDS,
        "arxiv": frozenset({"arxiv_category"}),
        "huggingface": frozenset({"huggingface_author"}),
        "bluesky": frozenset({"bluesky_actor"}),
        "reddit": frozenset({"reddit_subreddit"}),
        "mastodon": frozenset({"mastodon_instance_url", "mastodon_account", "mastodon_hashtag"}),
        "x": frozenset({"x_account", "x_query"}),
        "github_releases": frozenset({"github_owner", "github_repository"}),
        "github": frozenset(
            {
                "github_owner",
                "github_repository",
                "include_issues",
                "include_pulls",
                "include_commits",
                "include_releases",
                "github_history_days",
            }
        ),
        "telegram": frozenset({"telegram_chat_ids"}),
        "alpha_vantage": frozenset(
            {"market_symbols", "market_currency", "market_exchange_timezone"}
        ),
        "coingecko": frozenset({"coingecko_assets"}),
        "anilist_anime": frozenset({"media_ids"}),
        "anilist_manga": frozenset({"media_ids"}),

        "mal_anime": frozenset({"mal_mode", "mal_ranking_type", "mal_season_year", "mal_season"}),
        "mal_manga": frozenset({"mal_ranking_type"}),
        "google_books": frozenset({"google_books_query", "google_books_subject"}),
        "hardcover_books": frozenset({"hardcover_mode"}),
        "omdb_lookup": frozenset({"omdb_mode", "omdb_imdb_id", "omdb_title", "omdb_search"}),
        "openlibrary_books": frozenset({"openlibrary_mode", "openlibrary_subject", "openlibrary_query"}),
        "gutendex_books": frozenset({"gutendex_topic", "gutendex_search"}),
        "ann_news": frozenset({"ann_mode", "ann_media_type", "ann_category", "ann_edition"}),
        "mangaupdates_releases": frozenset({"series_ids"}),
        "mangadex_releases": frozenset({"manga_ids"}),
        "apple_books_rss": frozenset({"country", "kind", "chart"}),
        "podcast_rss": frozenset({"feed_url", "itunes_podcast_id"}),
        "wikidata_movies": frozenset({"lookback_days", "language"}),
        "itunes_movies": frozenset({"search_terms", "country"}),
        "letterboxd_rss": frozenset({"letterboxd_username"}),
        "youtube": YOUTUBE_SCOPE_FIELDS,
        "open_meteo": frozenset(
            {"weather_latitude", "weather_longitude", "weather_timezone", "weather_metrics"}
        ),
        "google_news": frozenset({"news_query", "news_site", "news_locale"}),
        "sec_edgar": frozenset({"sec_cik", "sec_contact_email"}),
        "openalex": frozenset({"scholarly_query"}),
        "fred": frozenset({"fred_series_id"}),
        "openweathermap": frozenset({"owm_latitude", "owm_longitude"}),
        "openaq": frozenset({"openaq_location_id"}),
        "nasa_firms": frozenset({"firms_country"}),
        "google_mail": frozenset({"google_mail_label_ids", "google_mail_history_days"}),
        "google_calendar": frozenset({"google_calendar_ids"}),
        "google_drive": frozenset({"google_drive_ids"}),
        "health": frozenset({"fhir_base_url", "fhir_patient_id"}),
        "home_assistant": frozenset({"ha_base_url", "ha_entity_ids"}),
        "notion": frozenset({"notion_page_ids"}),
        "slack": frozenset({"slack_channel_ids"}),
        **dict.fromkeys(FIXED_SCOPE_PROVIDERS, frozenset[str]()),
    }
)
NATIVE_PROVIDERS = frozenset(PROVIDER_SOURCE_TYPES)
GENERIC_CATALOG_SOURCE_TYPES = MappingProxyType(
    {
        "rss": "rss",
        "web": "web",
        "rest": "api",
        "mcp": "mcp",
    }
)
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
TRANSITION_PHASES = (
    "idle",
    "draining",
    "deactivating_old",
    "activating_new",
    "reconciliation_required",
)


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
