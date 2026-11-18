"""Provider id registries and ``source_fields`` allowlists/validators for typed provider records.

Split out of ``schemas`` (pure data and validation, no I/O, no imports from ``schemas``). ``schemas``
re-exports every name that other code imports, so existing import paths keep working.
"""

import math
import re
from datetime import date, datetime
from typing import Any

# Built-in feed-class presets (RSS/Atom and JSON event/list feeds): news-shaped records, no typed measurement.
BUILTIN_FEED_PROVIDER_IDS = (
    "aljazeera_world",
    "npr_news",
    "fed_press",
    "gdacs_rss",
    "lobsters",
    "github_trending",
    "wwr_jobs",
    "vietnamworks_jobs",
    "itviec_jobs",
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
)
# Keyed news-shaped presets (providers/keyed.py).
KEYED_FEED_PROVIDER_IDS = ("finnhub_news", "newsapi")
NEWS_PROVIDER_IDS = (
    *BUILTIN_FEED_PROVIDER_IDS,
    *KEYED_FEED_PROVIDER_IDS,
    "bbc_world",
    "vnexpress_business",
    "dantri_news",
    "thanhnien_news",
    "tuoitre_news",
    "hn_top",
    "gdelt_economy",
    "google_news",
    "openalex",
    "sec_edgar",
)
MEASUREMENT_PROVIDER_IDS = (
    "alpha_vantage",
    "open_meteo",
    "world_bank",
    "frankfurter",
    "ecb",
    "binance",
    "vn_stock",
    "alternative_me",
    "usgs",
    "coinpaprika",
    "coingecko",
    "cisa_kev",
    "nasa_eonet",
    "fred",
    "openweathermap",
    "openaq",
    "nasa_firms",
    "noaa_kp",
    "open_er_api",
    "oecd_cli",
    "iss_position",
    "disease_sh",
    "ioda",
    "nvd",
)
MEDIA_PROVIDER_IDS = (
    "wikidata_movies",
    "itunes_movies",
    "letterboxd_rss",
    "anilist_anime",
    "livechart_episodes",
    "ann_news",
    "anilist_manga",
    "mangaupdates_releases",
    "mangadex_releases",
    "openlibrary_books",
    "gutendex_books",
    "apple_books_rss",
    "podcast_rss",
    "tvmaze_schedule",
    "listenbrainz_releases",
    "steam_releases",
    "tmdb_movies",
    "tmdb_tv",
    "trakt_media",
    "omdb_lookup",
    "mal_anime",
    "mal_manga",
    "google_books",
    "hardcover_books",
    "youtube_api",
    "podcast_index",
    "igdb_games",
)
PROVIDER_IDS = (
    "youtube",
    "arxiv",
    "huggingface",
    "bluesky",
    "reddit",
    "mastodon",
    "x",
    "github_releases",
    "github",
    "telegram",
    "notion",
    "slack",
    "google_mail",
    "google_calendar",
    "google_drive",
    "health",
    "home_assistant",
    *MEASUREMENT_PROVIDER_IDS,
    *NEWS_PROVIDER_IDS,
    *MEDIA_PROVIDER_IDS,
)
# Bounded per-provider evidence keys retained beside the typed measurement (no arbitrary response mirroring).
MEASUREMENT_PROVIDER_FIELDS = {
    "alpha_vantage": {"date", "symbol", "open", "high", "low", "close", "volume"},
    "open_meteo": {"time", "timezone", "utc_offset_seconds", "latitude", "longitude"},
    "world_bank": {"country", "indicator", "period", "unit", "decimal_value"},
    "frankfurter": {"date", "base", "quote", "decimal_value", "providers"},
    "ecb": {"date", "base", "quote", "decimal_value"},
    "binance": {"symbol", "base", "quote", "decimal_value"},
    "vn_stock": {
        "symbol",
        "date",
        "kind",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "decimal_value",
    },
    "alternative_me": {"timestamp", "value_classification", "decimal_value"},
    "usgs": {
        "place",
        "type",
        "status",
        "magType",
        "depth_km",
        "latitude",
        "longitude",
        "decimal_mag",
        "event_time",
        "updated",
    },
    "cisa_kev": {
        "cveID",
        "vendorProject",
        "product",
        "dateAdded",
        "dueDate",
        "knownRansomwareCampaignUse",
    },
    "coinpaprika": {"id", "symbol", "last_updated", "quote", "decimal_value"},
    "coingecko": {"coin_id", "quote", "decimal_value", "last_updated_at"},
    "nasa_eonet": {
        "categories",
        "closed",
        "latitude",
        "longitude",
        "event_time",
        "magnitude_unit",
        "decimal_value",
    },
    "fred": {"series_id", "date", "decimal_value", "units"},
    "openweathermap": {"dt", "place", "decimal_value"},
    "openaq": {"location_id", "sensor_id", "datetime", "decimal_value"},
    "nasa_firms": {"acq", "confidence", "satellite", "daynight", "decimal_value"},
    "noaa_kp": {"time_tag", "a_running", "station_count", "decimal_value"},
    "open_er_api": {"date", "base", "quote", "decimal_value"},
    "oecd_cli": {"area", "period", "decimal_value"},
    "iss_position": {"velocity_kmh", "timestamp", "visibility", "decimal_value"},
    "disease_sh": {"updated", "decimal_value"},
    "ioda": {"location", "datasource", "start", "duration_s", "method"},
    "nvd": {"cveID", "status", "published", "lastModified", "severity", "decimal_value"},
}


_FEED_FIELDS = {
    "title",
    "summary",
    "canonical_url",
    "guid",
    "published_at",
    "provider_updated_at",
    "publisher",
    "image_url",
}
_MEDIA_FIELDS = _FEED_FIELDS | {
    "media_type",
    "artwork_aspect",
    "country",
    "language",
    "genres",
    "format",
    "content_rating",
    "release_status",
    "release_date",
    "release_year",
    "release_season",
    "rating",
    "creator",
    "tags",
    "season",
    "episode",
    "chapter",
    "volume",
    "duration_seconds",
    "page_count",
    "external_ids",
    "trailer_url",
    "attribution",
    "final_price",
    "price_currency",
    "release_date_text",
}


# Per-provider allowlist of retained source keys: the trust boundary for provider-controlled
# metadata. Adding a provider requires an explicit bounded field contract here.
_PROVIDER_SOURCE_FIELD_ALLOWLIST: dict[str, set[str]] = {
    "youtube": {
        "author",
        "summary",
        "title",
        "tags",
        "published_at",
        "provider_updated_at",
        "image_url",
        "rating",  # media:starRating average scaled to 0-100, kept so min_rating can filter
    },
    "arxiv": {
        "author",
        "authors",
        "categories",
        "tags",
        "summary",
        "title",
        "published_at",
        "provider_updated_at",
    },
    "huggingface": {"author", "tags", "pipeline_tag", "created_at", "last_modified"},
    "bluesky": {"author", "canonical_url", "created_at"},
    "reddit": {"title", "subreddit", "author", "canonical_url", "created_at"},
    "mastodon": {"author", "canonical_url", "created_at"},
    "x": {"author_id", "canonical_url", "created_at", "language"},
    "google_mail": {"thread_id", "history_id", "subject", "from", "to", "date_header"},
    "google_calendar": {
        "calendar_id",
        "event_id",
        "status",
        "start",
        "end",
        "start_timezone",
        "end_timezone",
        "recurrence",
        "html_link",
    },
    "google_drive": {"name", "mime_type", "modified_time", "web_view_link", "md5_checksum"},
    "notion": {"title", "canonical_url"},
    "slack": {"channel_id", "message_ts", "thread_ts", "user_id"},
    "health": {"patient_id", "observation_id", "value", "unit", "observed_at_source"},
    "home_assistant": {
        "entity_id",
        "state",
        "observed_at_source",
        "unit_of_measurement",
        "friendly_name",
        "latitude",
        "longitude",
    },
    "github_releases": {
        "node_id",
        "name",
        "body",
        "html_url",
        "tag_name",
        "draft",
        "prerelease",
        "author",
        "created_at",
        "published_at",
    },
    "github": {"record_type", "node_id", "html_url"},
    "telegram": set(),
    **{provider: set() for provider in MEASUREMENT_PROVIDER_IDS},
    **dict.fromkeys((*BUILTIN_FEED_PROVIDER_IDS, *KEYED_FEED_PROVIDER_IDS), _FEED_FIELDS),
    **dict.fromkeys(MEDIA_PROVIDER_IDS, _MEDIA_FIELDS),
    "bbc_world": _FEED_FIELDS,
    "vnexpress_business": _FEED_FIELDS,
    "tuoitre_news": _FEED_FIELDS,
    "thanhnien_news": _FEED_FIELDS,
    "dantri_news": _FEED_FIELDS,
    "google_news": _FEED_FIELDS,
    "sec_edgar": _FEED_FIELDS | {"form", "cik"},
    "openalex": {
        "title",
        "summary",
        "canonical_url",
        "published_at",
        "authors",
        "doi",
        "source",
        "cited_by_count",
    },
    "hn_top": {"title", "text", "url", "by", "time", "id", "type", "deleted", "dead"},
    "gdelt_economy": {"title", "url", "seendate", "domain", "language", "sourcecountry"},
}

# Maximum length of each string-typed source field (shared across providers).
_SOURCE_FIELD_STRING_LIMITS: dict[str, int] = {
    "author": 1000,
    "summary": 4000,
    "title": 500,
    "pipeline_tag": 128,
    "node_id": 256,
    "name": 500,
    "body": 4000,
    "html_url": 2048,
    "tag_name": 255,
    "record_type": 16,
    "last_modified": 64,
    "created_at": 64,
    "published_at": 64,
    "provider_updated_at": 64,
    "canonical_url": 2048,
    "guid": 512,
    "publisher": 200,
    "text": 4000,
    "url": 2048,
    "by": 128,
    "subreddit": 21,
    "author_id": 255,
    "type": 32,
    "form": 32,
    "cik": 10,
    "doi": 255,
    "source": 255,
    "seendate": 32,
    "domain": 255,
    "language": 64,
    "sourcecountry": 64,
    "channel_id": 82,
    "message_ts": 24,
    "thread_ts": 24,
    "user_id": 255,
    "thread_id": 255,
    "history_id": 255,
    "subject": 500,
    "from": 1000,
    "to": 2000,
    "date_header": 255,
    "calendar_id": 255,
    "event_id": 255,
    "status": 32,
    "start": 64,
    "end": 64,
    "start_timezone": 128,
    "end_timezone": 128,
    "recurrence": 100,
    "mime_type": 128,
    "modified_time": 64,
    "html_link": 2048,
    "web_view_link": 2048,
    "md5_checksum": 64,
    "patient_id": 128,
    "observation_id": 255,
    "value": 64,
    "unit": 64,
    "observed_at_source": 64,
    "entity_id": 255,
    "state": 512,
    "unit_of_measurement": 64,
    "friendly_name": 512,
    "latitude": 64,
    "longitude": 64,
    "media_type": 32,
    "artwork_aspect": 16,
    "country": 2,
    "format": 32,
    "content_rating": 32,
    "release_status": 32,
    "release_date": 64,
    "release_season": 32,
    "creator": 240,
    "season": 32,
    "episode": 32,
    "chapter": 32,
    "volume": 32,
    "attribution": 160,
    "price_currency": 3,
    "release_date_text": 64,
}

_MEDIA_TYPES = frozenset(
    {
        "movie",
        "anime",
        "manga",
        "manhwa",
        "manhua",
        "light_novel",
        "book",
        "audiobook",
        "youtube",
        "tv",
        "podcast",
        "music",
        "game",
    }
)
_ARTWORK_ASPECTS = frozenset({"poster", "square", "video", "landscape"})
_RELEASE_STATUSES = frozenset({"upcoming", "airing", "released", "completed", "hiatus"})
_MEDIA_EXTERNAL_ID_KEYS = frozenset(
    {
        "tmdb",
        "imdb",
        "anilist",
        "mal",
        "kitsu",
        "mangadex",
        "mangaupdates",
        "isbn13",
        "isbn10",
        "google_books",
        "openlibrary",
        "mbid",
        "tvmaze",
        "youtube",
        "steam",
        "igdb",
        "wikidata",
        "itunes",
        "trakt",
        "podcastindex",
        "hardcover",
    }
)
_MEDIA_INT_BOUNDS = {
    "release_year": (1800, 2100),
    "duration_seconds": (0, 604800),
    "page_count": (0, 50000),
}

_TIMESTAMP_FIELDS = frozenset(
    {
        "created_at",
        "published_at",
        "last_modified",
        "provider_updated_at",
        "modified_time",
        "observed_at_source",
    }
)


def _validate_string_list(key: str, value: Any) -> None:
    """Require a list of at most 100 strings, each within the per-key item bound."""
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(
            not isinstance(item, str) or len(item) > (1000 if key == "recurrence" else 255)
            for item in value
        )
    ):
        raise ValueError(f"Provider {key} must be a bounded string list")


def _validate_bool_field(key: str, value: Any) -> None:
    """Require a real boolean (ints do not qualify)."""
    if not isinstance(value, bool):
        raise ValueError(f"Provider {key} must be boolean")


def _validate_nonnegative_int(key: str, value: Any) -> None:
    """Require an exact non-negative ``int`` (booleans and numeric strings are rejected)."""
    if type(value) is not int or value < 0:
        raise ValueError(f"Provider {key} must be a non-negative integer")


def _validate_https_link(key: str, value: Any) -> None:
    """Require a bounded HTTPS URL with a host and without embedded credentials."""
    from urllib.parse import urlsplit

    parsed_link = urlsplit(value) if isinstance(value, str) and len(value) <= 2048 else None
    if (
        parsed_link is None
        or parsed_link.scheme != "https"
        or not parsed_link.hostname
        or parsed_link.username
        or parsed_link.password
    ):
        raise ValueError(f"Provider {key} must be an HTTPS link without credentials")


def _validate_release_url(key: str, value: Any) -> None:
    """Require a bounded HTTPS release URL that targets ``github.com`` without credentials."""
    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Provider release URL must use HTTPS")
    parsed_url = urlsplit(value)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "github.com"
        or parsed_url.username
        or parsed_url.password
    ):
        raise ValueError("Provider release URL must target github.com")


def _validate_iso_timestamp(key: str, value: str) -> None:
    """Require an ISO-8601 timestamp that carries a timezone (``Z`` is accepted)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Provider {key} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Provider {key} must include a timezone")


def _validate_calendar_boundary(key: str, value: str) -> None:
    """Require a calendar ``start``/``end`` as an ISO date (all-day) or an offset-bearing timestamp."""
    try:
        # Calendar all-day events use YYYY-MM-DD; timed events must carry an offset.
        parsed_date = date.fromisoformat(value) if "T" not in value else None
        parsed_time = (
            datetime.fromisoformat(value.replace("Z", "+00:00")) if parsed_date is None else None
        )
    except ValueError as exc:
        raise ValueError(f"Provider {key} must be an ISO date or timestamp") from exc
    if parsed_date is None and (
        parsed_time is None or parsed_time.tzinfo is None or parsed_time.utcoffset() is None
    ):
        raise ValueError(f"Provider {key} timestamp must include a timezone")


def _validate_media_genres(value: Any) -> None:
    """Require at most 12 lowercase ``[a-z0-9_]+`` genre slugs, each 1..48 characters."""
    if (
        not isinstance(value, list)
        or len(value) > 12
        or any(
            not isinstance(item, str)
            or not 1 <= len(item) <= 48
            or re.fullmatch(r"[a-z0-9_]+", item) is None
            for item in value
        )
    ):
        raise ValueError("Provider genres must be a bounded lowercase slug list")


def _validate_bounded_int(key: str, value: Any) -> None:
    """Require an exact ``int`` (not bool) inside ``_MEDIA_INT_BOUNDS[key]``."""
    lo, hi = _MEDIA_INT_BOUNDS[key]
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"Provider {key} must be a bounded integer")


def _validate_media_rating(value: Any) -> None:
    """Require a finite number (int or float, not bool) normalized within ``[0.0, 100.0]``."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 100.0
    ):
        raise ValueError("Provider rating must be a finite number between 0 and 100")


def _validate_external_ids(value: Any) -> None:
    """Require a bounded ``dict[str, str]`` (<= 16 entries) of allowlisted cross-provider IDs."""
    if (
        not isinstance(value, dict)
        or len(value) > 16
        or any(
            k not in _MEDIA_EXTERNAL_ID_KEYS
            or not isinstance(v, str)
            or not v.strip()
            or len(v) > 64
            for k, v in value.items()
        )
    ):
        raise ValueError("Provider external_ids must be a bounded identifier map")


def _validate_bounded_string(key: str, value: Any) -> None:
    """Require a string within the key's bound, plus timestamp/calendar/media enum format rules."""
    if not isinstance(value, str) or len(value) > _SOURCE_FIELD_STRING_LIMITS[key]:
        raise ValueError(f"Provider {key} must be a bounded string")
    if key in _TIMESTAMP_FIELDS:
        _validate_iso_timestamp(key, value)
    elif key in {"start", "end"}:
        _validate_calendar_boundary(key, value)
    elif key == "release_date":
        if re.fullmatch(r"\d{4}", value):
            _validate_calendar_boundary(key, value + "-01-01")
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            _validate_calendar_boundary(key, value + "-01")
        elif re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?", value
        ):
            _validate_calendar_boundary(key, value)
        else:
            raise ValueError("Provider release_date must be an ISO date or timestamp")
    elif key == "country" and re.fullmatch(r"[A-Z]{2}", value) is None:
        raise ValueError("Provider country must be an ISO 3166-1 alpha-2 code")
    elif key == "media_type" and value not in _MEDIA_TYPES:
        raise ValueError("Provider media_type is unsupported")
    elif key == "artwork_aspect" and value not in _ARTWORK_ASPECTS:
        raise ValueError("Provider artwork_aspect is unsupported")
    elif key == "release_status" and value not in _RELEASE_STATUSES:
        raise ValueError("Provider release_status is unsupported")


def _validate_source_field(key: str, value: Any) -> None:
    """Validate one allowlisted source field by its class; unknown keys are rejected.

    Dispatch order is significant and mirrors the original ``elif`` chain: lists, booleans,
    integers, ratings, external IDs, links, release URL, then bounded strings. Raises ``ValueError``
    (surfaced by pydantic as a validation error) with the exact field-class message.
    """
    if key in {"tags", "categories", "authors", "recurrence"}:
        _validate_string_list(key, value)
    elif key == "final_price":
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Provider final_price must be a nonnegative integer or null")
    elif key == "price_currency":
        _validate_bounded_string(key, value)
        if re.fullmatch(r"[A-Z]{3}", value) is None:
            raise ValueError("Provider price currency must be an uppercase ISO code")
    elif key == "genres":
        _validate_media_genres(value)
    elif key in {"draft", "prerelease", "deleted", "dead"}:
        _validate_bool_field(key, value)
    elif key in {"time", "id", "cited_by_count"}:
        _validate_nonnegative_int(key, value)
    elif key in _MEDIA_INT_BOUNDS:
        _validate_bounded_int(key, value)
    elif key == "rating":
        _validate_media_rating(value)
    elif key == "external_ids":
        _validate_external_ids(value)
    elif key in {"canonical_url", "url", "web_view_link", "html_link", "image_url", "trailer_url"}:
        _validate_https_link(key, value)
    elif key == "html_url":
        _validate_release_url(key, value)
    elif key in _SOURCE_FIELD_STRING_LIMITS:
        _validate_bounded_string(key, value)
    else:
        raise ValueError("Provider snapshot field is unsupported")
