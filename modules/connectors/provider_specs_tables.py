"""Static provider id sets and interval floors split out of ``provider_specs`` to keep it small.

Pure data, no I/O. ``provider_specs`` re-exports every public name, so callers keep importing
them from ``modules.connectors.provider_specs``.
"""

from dataclasses import dataclass
from typing import Literal

CHECKED_ON = "2026-10-07"

# Providers the shared executor can dispatch (ADAPTERS or registered SIMPLE_NATIVE providers).
# CoinGecko requires an owner-entered API key (connector_rest_credentials) and fails closed without one.
# Code presence only; runtime_verified stays False.
DISPATCHABLE = frozenset(
    {
        "bbc_world",
        "vnexpress_business",
        "dantri_news",
        "thanhnien_news",
        "tuoitre_news",
        "hn_top",
        "gdelt_economy",
        "world_bank",
        "frankfurter",
        "ecb",
        "binance",
        "alternative_me",
        "usgs",
        "coinpaprika",
        "vn_stock",
        "vietnamworks_jobs",
        "itviec_jobs",
        "coingecko",
        "google_news",
        "nasa_eonet",
        "cisa_kev",
        "sec_edgar",
        "openalex",
        "tvmaze_schedule",
        "livechart_episodes",
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
        "bluesky",
        # Built-in RSS/Atom world-data feeds (providers/news.py FEED_URLS).
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
        # Built-in table-driven JSON feeds (providers/json_feed.py JSON_FEEDS).
        "gdacs_events",
        "nws_alerts",
        "emsc_quakes",
        "launch_library",
        "wikipedia_mostread",
        "wikipedia_top_views",
        "bmkg_quakes",
        "satnogs_observations",
        "noaa_kp",
        "open_er_api",
        "oecd_cli",
        "iss_position",
        "disease_sh",
        "ioda",
        "nvd",  # bespoke keyless mappers
        # Keyed free-tier presets (providers/keyed.py); each needs an owner-entered key and fails closed without one.
        "fred",
        "finnhub_news",
        "newsapi",
        "openweathermap",
        "openaq",
        "nasa_firms",
        "google_books",
        "hardcover_books",
        "igdb_games",
        "listenbrainz_releases",
        "mal_anime",
        "mal_manga",
        "omdb_lookup",
        "podcast_index",
        "steam_releases",
        "tmdb_movies",
        "tmdb_tv",
        "trakt_media",
        "youtube_api",
    }
)


# Hard minimum collection interval (minutes) per provider, the single source of truth for save-time validation,
# the runtime clamp and the manual "Collect now" cooldown. Rule: unofficial/scraped endpoints and job boards 6 h;
# data published once a day 24 h; small community sites and strict free tiers 1 h; publisher feeds 30 min;
# real-time prices/hazards and owner-credential sources keep the 15-minute default (absent from the table).
PROVIDER_MIN_INTERVAL_MINUTES: dict[str, int] = {
    **dict.fromkeys(
        (
            "world_bank",
            "frankfurter",
            "ecb",
            "alternative_me",
            "open_er_api",
            "oecd_cli",
            "wikipedia_mostread",
            "wikipedia_top_views",
            "disease_sh",
            "openalex",
            "fred",
            "alpha_vantage",
        ),
        1440,
    ),
    **dict.fromkeys(
        (
            "vn_stock",
            "vietnamworks_jobs",
            "itviec_jobs",
            "wwr_jobs",
            "github_trending",
            "launch_library",
            "satnogs_observations",
            "us_travel_advisories",
            "nvd",
            "sec_edgar",
            "newsapi",
            "nasa_firms",
            "cisa_kev",
            "arxiv",
        ),
        360,
    ),
    **dict.fromkeys(
        (
            "lobsters",
            "fed_press",
            "noaa_kp",
            "ioda",
            "open_meteo",
            "github_releases",
            "youtube",
            "huggingface",
            "openweathermap",
            "openaq",
            "finnhub_news",
            "tvmaze_schedule",
            # Keyless media catalogs and release feeds: small community APIs and strict free tiers.
            "anilist_manga",
            "ann_news",
            "apple_books_rss",
            "gutendex_books",
            "itunes_movies",
            "letterboxd_rss",
            "mangadex_releases",
            "mangaupdates_releases",
            "openlibrary_books",
            "podcast_rss",
            "wikidata_movies",
        ),
        60,
    ),
    **dict.fromkeys(
        (
            "google_news",
            "gdelt_economy",
            "bluesky",
            "gdacs_rss",
            "gdacs_events",
            "emsc_quakes",
            "bmkg_quakes",
            "noaa_tsunami",
            "jma_eqvol",
            "bbc_world",
            "aljazeera_world",
            "npr_news",
            "vnexpress_business",
            "tuoitre_news",
            "thanhnien_news",
            "dantri_news",
            # Airing schedules whose spec default is 30 minutes (episode times shift close to broadcast).
            "anilist_anime",
            "livechart_episodes",
        ),
        30,
    ),
    "tmdb_movies": 360,
    "tmdb_tv": 360,
    "trakt_media": 360,
    "omdb_lookup": 1440,
    "mal_anime": 360,
    "mal_manga": 360,
    "google_books": 1440,
    "hardcover_books": 360,
    "youtube_api": 30,
    "podcast_index": 60,
    "steam_releases": 360,
    "listenbrainz_releases": 360,  # MusicBrainz asks for low request rates; releases change daily
    "igdb_games": 360,
}


_EXPANSION_IDS = frozenset(
    {
        "vn_stock",
        "vietnamworks_jobs",
        "itviec_jobs",
        "github_trending",
        "wwr_jobs",
        "npr_news",
        "fed_press",
        "gdacs_rss",
        "lobsters",
        "us_travel_advisories",
        "noaa_tsunami",
        "jma_eqvol",
        "gdacs_events",
        "nws_alerts",
        "emsc_quakes",
        "launch_library",
        "wikipedia_mostread",
        "wikipedia_top_views",
        "bmkg_quakes",
        "satnogs_observations",
        "noaa_kp",
        "open_er_api",
        "oecd_cli",
        "iss_position",
        "disease_sh",
        "ioda",
        "nvd",
        "fred",
        "finnhub_news",
        "newsapi",
        "openweathermap",
        "openaq",
        "nasa_firms",
    }
)
_REALTIME_TIER_IDS: frozenset[str] = frozenset(
    {
        "binance",
        "coinpaprika",
        "coingecko",
        "usgs",
        "nws_alerts",
        "emsc_quakes",
        "bmkg_quakes",
        "noaa_tsunami",
        "jma_eqvol",
        "iss_position",
        "gdacs_events",
        "noaa_kp",
        "openweathermap",
        "openaq",
        "nasa_firms",
    }
)
_LONG_RETENTION_IDS: frozenset[str] = frozenset(
    {
        "world_bank",
        "frankfurter",
        "ecb",
        "fred",
        "oecd_cli",
        "sec_edgar",
        "openalex",
        "cisa_kev",
        "nvd",
    }
)


@dataclass(frozen=True)
class QuotaWindowPolicy:
    """One durable budget window; ``limit_units=None`` means counted with no enforced cap."""

    policy_key: str
    budget_kind: Literal["provider", "credential", "ip"]
    window: Literal[
        "second", "minute", "day", "day_pacific", "month"
    ]  # day_pacific: midnight-to-midnight America/Los_Angeles (YouTube)
    unit: Literal["http_calls", "request_weight", "call_credits"]
    limit_units: int | None
    cost_per_send: int = 1
    basis: Literal["pilot_local", "official_documented", "official_unknown"] = "pilot_local"


# Durable call-count budgets for providers whose use is governed by connector credentials,
# local service capacity, or per-method API limits rather than free-provider terms. These do
# not imply entitlement; adapter-specific request bounds remain authoritative as well.
_NON_FREE_QUOTA_POLICIES: dict[str, tuple[QuotaWindowPolicy, ...]] = {
    provider_id: (
        QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 30),
        # Google mail's bounded first snapshot can read 500 messages plus profile/list-page overhead.
        QuotaWindowPolicy(
            "pilot_calls_utc_day",
            "provider",
            "day",
            "http_calls",
            600 if provider_id.startswith("google_") else 500,
        ),
    )
    for provider_id in (
        "health",
        "home_assistant",
        "google_mail",
        "google_calendar",
        "google_drive",
        "notion",
        "slack",
        "reddit",
    )
}

# Separate live-map pilot budgets are local ceilings, not statements of provider entitlement.
_NON_FREE_QUOTA_POLICIES.update(
    {
        "live_github_releases": (
            QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 10),
            QuotaWindowPolicy("pilot_calls_utc_day", "provider", "day", "http_calls", 48),
        ),
        **{
            provider_id: (
                QuotaWindowPolicy("pilot_calls_utc_minute", "provider", "minute", "http_calls", 10),
                QuotaWindowPolicy("pilot_calls_utc_day", "provider", "day", "http_calls", 96),
            )
            for provider_id in ("live_vnexpress_world", "live_vnexpress_news", "live_gaming_news")
        },
        "live_openweather_tiles": (
            QuotaWindowPolicy("pilot_calls_utc_minute", "credential", "minute", "http_calls", 60),
            QuotaWindowPolicy("pilot_calls_utc_day", "credential", "day", "http_calls", 600),
        ),
    }
)


@dataclass(frozen=True)
class FreeProviderSpec:
    """Catalog facts and execution bounds; availability is not a license grant."""

    id: str
    hosts: tuple[str, ...]
    data_kind: Literal["news", "measurement", "event"]
    key_required: bool
    default_interval_minutes: int
    terms_url: str
    eligibility: Literal["open", "personal", "noncommercial", "review"]
    attribution: str
    execution: Literal["supported", "experimental", "research_only"]
    label: str = ""
    endpoints: tuple[str, ...] = ()
    quota: tuple[QuotaWindowPolicy, ...] = ()
    # Evidence kinds stay separate; no field implies another. Adapters ship in P2/P3/C3.
    endpoint_probe: Literal["plan_documented", "unverified"] = "plan_documented"
    code_implemented: bool = False
    runtime_verified: bool = False
    checked_on: str = CHECKED_ON
    secret_location: Literal["header", "query", "path"] | None = None
    category: Literal["news", "economy", "hazards", "space", "security", "tech", "media"] = "news"
    realtime_tier: Literal["realtime", "near_realtime"] = "near_realtime"
    skip_embedding: bool = True
    default_retention_days: int | None = 60


def _unknown(kind: str = "provider", unit: str = "http_calls", cost: int = 1) -> QuotaWindowPolicy:
    """Build the conservative quota window for a provider whose official daily limit is unpublished."""
    return QuotaWindowPolicy(
        "official_unknown_calls_utc_day",
        kind,  # type: ignore[arg-type]
        "day",
        unit,  # type: ignore[arg-type]
        None,
        cost,
        "official_unknown",
    )


def _day(
    limit: int,
    kind: str = "provider",
    unit: str = "http_calls",
    cost: int = 1,
    key: str = "pilot_calls_utc_day",
) -> QuotaWindowPolicy:
    """Build a UTC-day quota window with a fixed pilot ``limit`` of ``cost``-weighted calls."""
    return QuotaWindowPolicy(key, kind, "day", unit, limit, cost)  # type: ignore[arg-type]


def _spec(
    id_: str,
    label: str,
    category: str,
    hosts: tuple[str, ...],
    kind: str,
    interval: int,
    terms_url: str,
    eligibility: str,
    attribution: str,
    endpoints: tuple[str, ...],
    quota: tuple[QuotaWindowPolicy, ...],
    execution: str = "supported",
    key: bool = False,
    secret: str | None = None,
    implemented: bool = False,
    checked_on: str = CHECKED_ON,
) -> FreeProviderSpec:
    """Build one free-provider spec; ``code_implemented`` is true when flagged or the id is dispatchable."""
    return FreeProviderSpec(
        id=id_,
        hosts=hosts,
        data_kind=kind,  # type: ignore[arg-type]
        key_required=key,
        default_interval_minutes=interval,
        terms_url=terms_url,
        eligibility=eligibility,  # type: ignore[arg-type]
        attribution=attribution,
        execution=execution,  # type: ignore[arg-type]
        label=label,
        category=category,  # type: ignore[arg-type]
        endpoints=endpoints,
        quota=quota,
        secret_location=secret,  # type: ignore[arg-type]
        code_implemented=implemented or id_ in DISPATCHABLE,
        checked_on=checked_on,
    )
