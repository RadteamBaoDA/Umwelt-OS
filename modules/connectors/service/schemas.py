"""Connector DTOs, configuration model and snapshots exposed through ``public``.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, TypeGuard
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from core.workspaces.schemas import AccessFence
from modules.connectors.backends import (
    TOPIC_WATCH_FIELDS as TOPIC_WATCH_FIELDS,
)
from modules.connectors.backends import (
    is_native_provider as _is_native_provider,
)
from modules.connectors.provider_options import (
    ISO_COUNTRY_PATTERN,
    MAX_COINS,
    MAX_CONTENT_RATINGS,
    MAX_JOB_KEYWORDS,
    MAX_MEDIA_COUNTRIES,
    MAX_MEDIA_FORMATS,
    MAX_MEDIA_GENRES,
    MAX_MEDIA_LANGUAGES,
    MAX_VN_SYMBOLS,
    AniListMangaCountry,
    AniListMediaId,
    AniListMode,
    AnimeSeason,
    CoinId,
    ContentRating,
    CryptoPair,
    EonetCategory,
    EonetStatus,
    FxCurrency,
    GdacsAlert,
    GdacsType,
    GdeltCountry,
    GdeltLang,
    GdeltTimespan,
    HnList,
    IsoCountry,
    JobCity,
    JobKeyword,
    JobLevel,
    MalAnimeRanking,
    MalMangaRanking,
    MediaFormat,
    MediaGenre,
    MediaLanguage,
    MediaStatus,
    MediaTag,
    NvdSeverity,
    NwsArea,
    NwsSeverity,
    OecdArea,
    Region,
    SatnogsStatus,
    UsgsMagnitude,
    UsgsPeriod,
    VnSymbol,
    WbIndicator,
    WikiLanguage,
    YoutubeFilter,
    clean_query,
)
from modules.connectors.sections import MAX_SECTIONS
from modules.ingestion.public import IngestionRecord, TelegramRawDelivery
from modules.sources.public import SourceFence

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"

WikidataQid = Annotated[str, StringConstraints(pattern=r"^Q[1-9][0-9]{0,11}$")]
TwoLetterLanguage = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{2}$")]
PositiveSeriesId = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]


DEFAULT_OVERLAP = timedelta(days=1)


class ProviderRateLimited(RuntimeError):
    """Signal provider throttling with a safe shared next-eligible instant."""

    def __init__(self, next_eligible_at: datetime) -> None:
        """Carry only a timezone-aware UTC deadline, never provider response text."""
        if next_eligible_at.tzinfo is None or next_eligible_at.utcoffset() is None:
            raise ValueError("Provider retry deadline must be timezone-aware")
        self.next_eligible_at = next_eligible_at.astimezone(UTC)
        super().__init__("Provider rate limit reached")


@dataclass(frozen=True)
class NativeCredentialSnapshot:
    """Detach Telegram reservation and immutable source-binding identity without ORM access."""

    access_fence: AccessFence
    workspace_id: UUID
    source_id: UUID
    operation_id: UUID
    source_generation: int
    configuration_revision: int
    verified_bot_id: str | None
    bound_bot_id: str | None
    encrypted_token: str | None
    state: str
    validated_at: datetime | None


@dataclass(frozen=True)
class NativeCredentialRevocationSnapshot:
    """Detach only original access, exact paused Source/revision and actual native operation.

    Internal removal observation, never HTTP authority or provider-send/readiness proof.
    operation_id=None means genuinely locked absence after valid parent proof; stale stored
    native generations/revisions still yield their actual operation. No secrets/bot binding.
    """

    access_fence: AccessFence
    source_fence: SourceFence
    connector_revision: int
    operation_id: UUID | None


class TelegramScopeValidation(BaseModel):
    """Represent successful Bot API identity and all configured channel checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified_bot_id: str = Field(pattern=r"^[0-9]{1,20}$")
    verified_chat_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    validated_at: datetime

    @field_validator("validated_at")
    @classmethod
    def aware_validation_time(cls, value: datetime) -> datetime:
        """Require the validation event timestamp to represent an aware instant."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Validation time must be timezone-aware")
        return value.astimezone(UTC)


class TelegramUpdatePage(BaseModel):
    """Represent one bounded Bot API response with its actual transport byte count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deliveries: tuple[TelegramRawDelivery, ...] = Field(max_length=100)
    collected_at: datetime
    transport_bytes: int = Field(gt=0, le=10 * 1024 * 1024)

    @field_validator("collected_at")
    @classmethod
    def aware_collection_time(cls, value: datetime) -> datetime:
        """Normalize provider response collection time to an aware UTC instant."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Collection time must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def limit_page_bytes(self) -> "TelegramUpdatePage":
        """Keep detached update pages under the provider transport memory bound."""
        if len(self.model_dump_json().encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("Telegram update page exceeds the byte limit")
        return self


def is_native_provider(provider: str | None) -> TypeGuard[str]:
    """Preserve the public trusted-provider guard through the shared identity registry."""
    return _is_native_provider(provider)


class CoinGeckoAsset(BaseModel):
    """One selected catalog coin; its ID is the stable source identity, not its reusable ticker."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    series_symbol: str | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        """Keep catalog display names printable and trimmed before they become stored UI labels."""
        if value != value.strip() or not value.isprintable():
            raise ValueError("CoinGecko asset name must be printable and trimmed")
        return value

    @field_validator("symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        """Keep the provider's displayed ticker printable and single-line; it never enters request URLs."""
        if value != value.strip() or not value.isprintable():
            raise ValueError("CoinGecko symbol must be printable and trimmed")
        return value

    @model_validator(mode="after")
    def set_series_symbol(self) -> "CoinGeckoAsset":
        """Derive the stable observation key; reject a client-supplied key that differs from the ID."""
        from modules.connectors.providers.crypto import coingecko_series_symbol

        expected = coingecko_series_symbol(self.id)
        if self.series_symbol is not None and self.series_symbol != expected:
            raise ValueError("CoinGecko series symbol does not match the coin ID")
        self.series_symbol = expected
        return self


def _lower_host(value: object) -> object:
    """Strip and lowercase a hostname before the pattern check so "EXAMPLE.com" is accepted."""
    return value.strip().lower() if isinstance(value, str) else value


# Lowercased hostname for Topic watch site filters (dot-boundary suffix match happens in the collector).
SiteHost = Annotated[
    str, BeforeValidator(_lower_host), StringConstraints(max_length=253, pattern=r"^[a-z0-9.-]+$")
]


class ConnectorConfig(BaseModel):
    """Validate bounded source scopes, per-provider mappings, timezone, and schedule settings.

    Social, Google, FHIR, Home Assistant, Notion, and Slack selections are checked against the
    provider-specific field bounds before they can be persisted or activated. YouTube channel IDs,
    handle lists, and playlist lists have format and uniqueness checks before server-side resolution.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl | None = None
    feed_url: HttpUrl | None = None
    js_render: bool = False
    max_pages: int = Field(default=10, ge=1, le=10)
    max_depth: int = Field(default=2, ge=0, le=2)
    timeout_seconds: int = Field(default=60, ge=1, le=60)
    items_path: str | None = Field(default=None, max_length=256)
    id_field: str | None = Field(default=None, max_length=128)
    title_field: str | None = Field(default=None, max_length=128)
    content_field: str | None = Field(default=None, max_length=128)
    updated_field: str | None = Field(default=None, max_length=128)
    timezone: str = DEFAULT_TIMEZONE
    schedule_interval_minutes: Literal[15, 30, 60, 360, 1440] | None = None
    youtube_channel_id: str | None = Field(default=None, pattern=r"^UC[A-Za-z0-9_-]{22}$")
    youtube_channels: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    arxiv_category: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9.-]{0,63}$"
    )
    huggingface_author: str | None = Field(
        default=None, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$"
    )
    bluesky_actor: str | None = Field(default=None, min_length=3, max_length=253)
    telegram_public_channel: str | None = Field(default=None, min_length=5, max_length=512)
    reddit_subreddit: str | None = Field(default=None, min_length=2, max_length=21)
    mastodon_instance_url: str | None = Field(default=None, min_length=8, max_length=512)
    mastodon_account: str | None = Field(default=None, min_length=1, max_length=320)
    mastodon_hashtag: str | None = Field(default=None, min_length=1, max_length=100)
    x_account: str | None = Field(default=None, min_length=1, max_length=15)
    x_query: str | None = Field(default=None, min_length=1, max_length=512)
    github_owner: str | None = Field(
        default=None, min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$"
    )
    github_repository: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]{1,100}$"
    )
    include_issues: bool = False
    include_pulls: bool = False
    include_commits: bool = False
    include_releases: bool = False
    github_history_days: StrictInt = Field(default=90, ge=1, le=365)
    telegram_chat_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    market_symbols: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=5)
    coingecko_assets: tuple["CoinGeckoAsset", ...] | None = Field(
        default=None, min_length=1, max_length=20
    )
    market_currency: str | None = Field(
        default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"
    )
    market_exchange_timezone: str | None = Field(default=None, min_length=1, max_length=64)
    weather_latitude: float | None = Field(default=None, ge=-90, le=90)
    weather_longitude: float | None = Field(default=None, ge=-180, le=180)
    weather_timezone: str | None = Field(default=None, min_length=1, max_length=64)
    weather_metrics: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    news_query: str | None = Field(default=None, min_length=1, max_length=200)
    news_site: Literal["any", "reuters.com", "apnews.com", "bbc.com", "vnexpress.net"] | None = None
    news_locale: Literal["vi-VN", "en-US"] | None = None
    sec_cik: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    sec_contact_email: str | None = Field(
        default=None,
        max_length=254,
        pattern=r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$",
    )
    scholarly_query: str | None = Field(default=None, min_length=1, max_length=200)
    fred_series_id: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,25}$")
    owm_latitude: float | None = Field(default=None, ge=-90, le=90)
    owm_longitude: float | None = Field(default=None, ge=-180, le=180)
    openaq_location_id: StrictInt | None = Field(default=None, ge=1, le=2_147_483_647)
    firms_country: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    google_mail_label_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=10)
    google_mail_history_days: StrictInt = Field(default=30, ge=1, le=30)
    google_calendar_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=25)
    google_drive_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    notion_page_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    slack_channel_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    fhir_base_url: str | None = Field(default=None, min_length=8, max_length=512)
    fhir_patient_id: str | None = Field(default=None, min_length=1, max_length=128)
    ha_base_url: str | None = Field(default=None, min_length=8, max_length=512)
    ha_entity_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=500)
    history_mode: Literal["returned_snapshot", "pending_updates"] | None = None
    realtime_mode: Literal["realtime", "near_realtime"] | None = None
    retention_days: StrictInt | None = Field(default=None, ge=1, le=3650)
    skip_embedding: StrictBool | None = None
    # Per-sync selection options (INCREMENTAL_PROVIDERS only; registry rejects them elsewhere); None = today's behaviour.
    max_items: StrictInt | None = Field(default=None, ge=1, le=100)
    include_keywords: tuple[str, ...] | None = Field(default=None, max_length=20)
    exclude_keywords: tuple[str, ...] | None = Field(default=None, max_length=20)
    max_age_days: StrictInt | None = Field(default=None, ge=1, le=365)
    # RSS section slugs (providers with a catalog only; the registry checks membership per provider).
    feed_sections: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_SECTIONS
    )
    # Wave D per-type options (enums/bounded values; the registry allows each only on its own provider).
    hn_list: HnList | None = None
    gdelt_query: str | None = Field(default=None, min_length=1, max_length=100)
    gdelt_timespan: GdeltTimespan | None = None
    gdelt_source_lang: GdeltLang | None = None
    gdelt_source_country: GdeltCountry | None = None
    usgs_min_magnitude: UsgsMagnitude | None = None
    usgs_period: UsgsPeriod | None = None
    min_magnitude: float | None = Field(default=None, ge=2.0, le=8.0)
    eonet_categories: tuple[EonetCategory, ...] | None = Field(
        default=None, min_length=1, max_length=13
    )
    eonet_status: EonetStatus | None = None
    eonet_days: StrictInt | None = Field(default=None, ge=1, le=30)
    gdacs_event_types: tuple[GdacsType, ...] | None = Field(
        default=None, min_length=1, max_length=6
    )
    gdacs_alert_levels: tuple[GdacsAlert, ...] | None = Field(
        default=None, min_length=1, max_length=3
    )
    nws_severity: tuple[NwsSeverity, ...] | None = Field(default=None, min_length=1, max_length=4)
    nws_area: NwsArea | None = None
    nvd_severity: tuple[NvdSeverity, ...] | None = Field(default=None, min_length=1, max_length=4)
    nvd_window_days: StrictInt | None = Field(default=None, ge=1, le=7)
    nvd_keyword: str | None = Field(default=None, min_length=1, max_length=100)
    kev_ransomware_only: StrictBool | None = None
    ioda_country: str | None = Field(default=None, pattern=ISO_COUNTRY_PATTERN)
    region: Region | None = None
    # Wave F per-type options (curated enums/bounded numbers; the registry allows each only on its own provider).
    wiki_language: WikiLanguage | None = None
    satnogs_status: SatnogsStatus | None = None
    disease_country: IsoCountry | None = None
    wb_country: IsoCountry | None = None
    wb_indicator: WbIndicator | None = None
    fx_base: FxCurrency | None = None
    fx_quotes: tuple[FxCurrency, ...] | None = Field(default=None, min_length=1, max_length=10)
    oecd_countries: tuple[OecdArea, ...] | None = Field(default=None, min_length=1, max_length=10)
    crypto_symbols: tuple[CryptoPair, ...] | None = Field(default=None, min_length=1, max_length=10)
    vn_symbols: tuple[VnSymbol, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_VN_SYMBOLS
    )
    job_keywords: tuple[JobKeyword, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_JOB_KEYWORDS
    )
    job_cities: tuple[JobCity, ...] | None = Field(default=None, min_length=1, max_length=3)
    job_levels: tuple[JobLevel, ...] | None = Field(default=None, min_length=1, max_length=5)
    job_min_salary_usd: StrictInt | None = Field(default=None, ge=0, le=20000)
    coin_ids: tuple[CoinId, ...] | None = Field(default=None, min_length=1, max_length=MAX_COINS)
    kp_min: float | None = Field(default=None, ge=0, le=9)
    # Topic watch scope (the registry requires topic, max items and token budget on that provider only).
    topic_watch_topic: str | None = Field(default=None, min_length=1, max_length=500)
    topic_watch_keep_rule: str | None = Field(default=None, max_length=500)
    topic_watch_languages: list[Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]] | None = (
        Field(default=None, max_length=4)
    )
    topic_watch_include_sites: list[SiteHost] | None = Field(default=None, max_length=10)
    topic_watch_exclude_sites: list[SiteHost] | None = Field(default=None, max_length=10)
    topic_watch_max_items: StrictInt | None = Field(default=None, ge=1, le=10)
    topic_watch_token_budget: StrictInt | None = Field(default=None, ge=1, le=200_000)
    topic_watch_model_role: Literal["background", "chat", "brief"] | None = None
    advisory_min_level: StrictInt | None = Field(default=None, ge=1, le=4)
    # Multi-dimensional media filter options & YouTube scope upgrades (all default to None).
    media_country: IsoCountry | None = None
    media_countries: tuple[IsoCountry, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_MEDIA_COUNTRIES
    )
    media_genres: tuple[MediaGenre, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_MEDIA_GENRES
    )
    media_languages: tuple[MediaLanguage, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_MEDIA_LANGUAGES
    )
    media_formats: tuple[MediaFormat, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_MEDIA_FORMATS
    )
    release_year: StrictInt | None = Field(default=None, ge=1800, le=2100)
    anime_season: AnimeSeason | None = None
    media_ids: tuple[AniListMediaId, ...] | None = Field(default=None, min_length=1, max_length=50)
    anilist_mode: AniListMode | None = None
    openlibrary_mode: Literal["subject", "search", "recent"] | None = None
    openlibrary_subject: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _-]+$"
    )
    openlibrary_query: str | None = Field(default=None, min_length=1, max_length=200)
    gutendex_topic: str | None = Field(default=None, min_length=1, max_length=120)
    gutendex_search: str | None = Field(default=None, min_length=1, max_length=200)
    gutendex_copyright: StrictBool | None = None
    gutendex_mime_type: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+$"
    )
    gutendex_author_year_start: StrictInt | None = Field(default=None, ge=0, le=2100)
    gutendex_author_year_end: StrictInt | None = Field(default=None, ge=0, le=2100)
    ebooks_only: StrictBool | None = None
    country_of_origin: AniListMangaCountry | None = None
    media_tags: tuple[MediaTag, ...] | None = Field(default=None, min_length=1, max_length=16)
    media_statuses: tuple[MediaStatus, ...] | None = Field(default=None, min_length=1, max_length=5)
    content_ratings: tuple[ContentRating, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_CONTENT_RATINGS
    )
    min_rating: float | None = Field(
        default=None, ge=0.0, le=100.0, allow_inf_nan=False, strict=True
    )
    exclude_adult: StrictBool | None = None
    tmdb_mode: (
        Literal["now_playing", "upcoming", "changes", "airing_today", "on_the_air"] | None
    ) = None
    tmdb_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=16,
        pattern=r"^[a-z]{2}(?:-[a-z0-9]{2,8})?$",
        strict=True,
    )
    tmdb_certification_country: IsoCountry | None = None
    tmdb_certification: str | None = Field(
        default=None, min_length=1, max_length=12, pattern=r"^[A-Za-z0-9+-]+$", strict=True
    )
    trakt_mode: (
        Literal[
            "movies_trending",
            "movies_anticipated",
            "shows_trending",
            "shows_anticipated",
            "movies_calendar",
            "shows_calendar",
            "movies_search",
            "shows_search",
        ]
        | None
    ) = None
    trakt_query: str | None = Field(default=None, min_length=1, max_length=100, strict=True)
    trakt_calendar_days: StrictInt | None = Field(default=None, ge=1, le=31)
    trakt_certifications: tuple[Literal["G", "PG", "PG-13", "R", "NC-17"], ...] | None = Field(
        default=None, min_length=1, max_length=5
    )
    omdb_mode: Literal["imdb_id", "title", "search"] | None = None
    omdb_imdb_id: str | None = Field(
        default=None, min_length=9, max_length=14, pattern=r"^tt\d{7,12}$", strict=True
    )
    omdb_title: str | None = Field(default=None, min_length=1, max_length=120, strict=True)
    omdb_search: str | None = Field(default=None, min_length=1, max_length=120, strict=True)
    omdb_type: Literal["movie", "series", "episode"] | None = None
    mal_mode: Literal["ranking", "season"] | None = None
    mal_ranking_type: MalAnimeRanking | MalMangaRanking | None = None
    mal_season_year: StrictInt | None = Field(default=None, ge=1917, le=2100)
    mal_season: AnimeSeason | None = None
    google_books_query: str | None = Field(default=None, min_length=1, max_length=160, strict=True)
    google_books_subject: str | None = Field(
        default=None, min_length=1, max_length=100, strict=True
    )
    google_books_print_type: Literal["all", "books", "magazines"] | None = None
    google_books_filter: Literal["ebooks", "free-ebooks", "paid-ebooks"] | None = None
    hardcover_mode: Literal["catalog", "upcoming"] | None = None
    tvmaze_mode: Literal["schedule_country", "schedule_web", "followed_shows"] | None = None
    tvmaze_show_ids: tuple[PositiveSeriesId, ...] | None = Field(
        default=None, min_length=1, max_length=20
    )
    tvmaze_global: StrictBool | None = None
    ann_mode: Literal["news", "encyclopedia"] | None = None
    ann_media_type: Literal["anime", "manga"] | None = None
    ann_category: (
        Literal["news", "review", "feature", "interview", "press-release", "all"] | None
    ) = None
    ann_edition: Literal["us", "w"] | None = None
    mangaupdates_types: (
        tuple[Literal["Manga", "Manhwa", "Manhua", "Novel", "Doujinshi"], ...] | None
    ) = Field(default=None, min_length=1, max_length=5)
    licensed_only: StrictBool | None = None
    exclude_genre: tuple[MediaGenre, ...] | None = Field(default=None, min_length=1, max_length=16)
    series_ids: tuple[PositiveSeriesId, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )
    manga_ids: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=100)
    original_languages: tuple[Literal["ja", "ko", "zh", "zh-hk"], ...] | None = Field(
        default=None, min_length=1, max_length=4
    )
    available_translated_languages: (
        tuple[Literal["en", "ja", "ko", "zh", "zh-hk", "vi", "fr", "de", "es"], ...] | None
    ) = Field(default=None, min_length=1, max_length=9)
    mangadex_content_ratings: (
        tuple[Literal["safe", "suggestive", "erotica", "pornographic"], ...] | None
    ) = Field(default=None, min_length=1, max_length=4)
    mangadex_statuses: tuple[Literal["ongoing", "completed", "hiatus", "cancelled"], ...] | None = (
        Field(default=None, min_length=1, max_length=4)
    )
    publication_demographic: (
        tuple[Literal["shounen", "shoujo", "josei", "seinen", "none"], ...] | None
    ) = Field(default=None, min_length=1, max_length=5)
    updated_at_since: AwareDatetime | None = None
    excluded_tags: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=100)
    country: IsoCountry | None = None
    kind: Literal["books", "audio-books"] | None = None
    chart: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    podcast_index_query: str | None = Field(default=None, min_length=1, max_length=160)
    podcast_index_languages: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=12
    )
    podcast_index_categories: str | None = Field(default=None, min_length=1, max_length=160)
    podcast_index_excluded_categories: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    podcast_index_since: StrictInt | None = Field(default=None, ge=1, le=4_102_444_800)
    podcast_index_value: Literal["any", "lightning", "hive", "webmonetization"] | None = None
    listenbrainz_username: str | None = Field(
        default=None, min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$"
    )
    listenbrainz_days: StrictInt | None = Field(default=None, ge=1, le=90)
    listenbrainz_sort: Literal["release_date", "release_group_date"] | None = None
    listenbrainz_tags: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    listenbrainz_status: Literal["official", "promotion", "bootleg"] | None = None
    steam_mode: Literal["featured", "news"] | None = None
    steam_country: IsoCountry | None = None
    steam_language: (
        Literal[
            "english",
            "vietnamese",
            "japanese",
            "schinese",
            "koreana",
            "french",
            "german",
            "spanish",
        ]
        | None
    ) = None
    steam_appids: tuple[Annotated[StrictInt, Field(gt=0)], ...] | None = Field(
        default=None, min_length=1, max_length=8
    )
    igdb_game_types: (
        tuple[
            Literal["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"], ...
        ]
        | None
    ) = Field(default=None, min_length=1, max_length=8)
    igdb_region: (
        Literal[
            "europe", "north_america", "australia", "new_zealand", "japan", "china", "asia", "world"
        ]
        | None
    ) = None
    igdb_platforms: tuple[Annotated[StrictInt, Field(gt=0)], ...] | None = Field(
        default=None, min_length=1, max_length=16
    )
    itunes_podcast_id: StrictInt | None = Field(default=None, ge=1, le=9_999_999_999_999_999)
    lookback_days: StrictInt | None = Field(default=None, ge=1, le=3650)
    genre_qids: tuple[WikidataQid, ...] | None = Field(default=None, min_length=1, max_length=12)
    language: TwoLetterLanguage | None = None
    search_terms: str | None = Field(default=None, min_length=1, max_length=100)
    letterboxd_username: str | None = Field(
        default=None, min_length=1, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$"
    )
    youtube_handles: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    youtube_playlist_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    youtube_filter: YoutubeFilter | None = None
    youtube_api_mode: Literal["uploads", "most_popular"] | None = None
    youtube_video_category_id: str | None = Field(default=None, pattern=r"^[0-9]{1,4}$")

    @field_validator("youtube_channels")
    @classmethod
    def valid_youtube_channels(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Require unique canonical channel IDs so each configured target has one stable feed identity."""
        if value is None:
            return None
        if any(not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel) for channel in value):
            raise ValueError("youtube_channels must contain canonical channel IDs")
        if len(set(value)) != len(value):
            raise ValueError("youtube_channels must be unique")
        return value

    @field_validator("youtube_handles")
    @classmethod
    def valid_youtube_handles(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Normalize handles and reject duplicate or malformed public channel names."""
        if value is None:
            return None
        handles = tuple(handle.strip().removeprefix("@").lower() for handle in value)
        if any(not re.fullmatch(r"[a-z0-9._-]{3,30}", handle) for handle in handles):
            raise ValueError("youtube_handles contains an invalid handle")
        if len(set(handles)) != len(handles):
            raise ValueError("youtube_handles must be unique")
        return handles

    @field_validator("youtube_playlist_ids")
    @classmethod
    def valid_youtube_playlists(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Accept bounded playlist identifiers only; arbitrary URLs never reach the request builder."""
        if value is None:
            return None
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{10,64}", playlist) for playlist in value):
            raise ValueError("youtube_playlist_ids contains an invalid playlist ID")
        if len(set(value)) != len(value):
            raise ValueError("youtube_playlist_ids must be unique")
        return value

    @field_validator("gdelt_query", "nvd_keyword", mode="before")
    @classmethod
    def clean_native_query(cls, value: object) -> object:
        """Sanitize free query text like ``news_query``; operators that bypass the enum selectors are refused."""
        return clean_query(value) if isinstance(value, str) else value

    @field_validator("feed_sections")
    @classmethod
    def unique_sections(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Reject duplicate section slugs so one GET per section is the quota contract."""
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Sections must be unique")
        return value

    @field_validator("include_keywords", "exclude_keywords", mode="before")
    @classmethod
    def clean_keywords(cls, value: object) -> object:
        """Strip each term, require 1..64 characters, and normalise an empty list to ``None`` (no filter)."""
        if not isinstance(value, (list, tuple)):
            return value
        terms = tuple(t.strip() if isinstance(t, str) else t for t in value)
        if any(isinstance(t, str) and not 1 <= len(t) <= 64 for t in terms):
            raise ValueError("Keywords must be 1 to 64 characters")
        return terms or None

    @field_validator("news_query", mode="before")
    @classmethod
    def clean_news_query(cls, value: object) -> object:
        """Strip control characters and fold whitespace; a bare ``site:`` operator would bypass the site allowlist."""
        if not isinstance(value, str):
            return value
        cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
        if re.search(r"(?i)\bsite\s*:", cleaned):
            raise ValueError("Use the site selector instead of a site: operator")
        return cleaned

    @field_validator("scholarly_query", mode="before")
    @classmethod
    def clean_scholarly_query(cls, value: object) -> object:
        """Strip control characters and fold whitespace so the stored OpenAlex query matches what is sent."""
        if not isinstance(value, str):
            return value
        return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Require a timezone identifier recognized by the installed IANA database."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("arxiv_category")
    @classmethod
    def valid_arxiv_category(cls, value: str | None) -> str | None:
        """Reject category separators that could broaden the fixed arXiv path."""
        if value is not None and any(char in value for char in "+,/"):
            raise ValueError("arXiv category must be one category")
        return value

    @field_validator("bluesky_actor")
    @classmethod
    def valid_bluesky_actor(cls, value: str | None) -> str | None:
        """Accept one Bluesky handle or supported DID, preventing arbitrary actor expressions."""
        if value is None:
            return None
        if re.fullmatch(r"(?i)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
            return value.lower()
        if re.fullmatch(r"did:plc:[a-z2-7]{24}", value):
            return value
        if re.fullmatch(r"did:web:[A-Za-z0-9.%:-]{1,220}", value):
            return value
        raise ValueError("Bluesky actor must be a handle or DID")

    @field_validator("telegram_public_channel")
    @classmethod
    def valid_telegram_public_channel(cls, value: str | None) -> str | None:
        """Normalize a channel username or t.me link to the bare username, rejecting anything else.

        Accepts ``name``, ``@name``, ``t.me/name``, ``https://t.me/name`` and ``https://t.me/s/name``;
        paths beyond the name, queries and other hosts fail so owner input cannot steer the fetch.
        """
        if value is None:
            return None
        match = re.fullmatch(
            r"(?:(?:https?://)?(?:www\.)?t\.me/(?:s/)?|@)?([A-Za-z][A-Za-z0-9_]{4,31})/?",
            value.strip(),
        )
        if match is None:
            raise ValueError("Telegram channel must be a public username or t.me link")
        return match.group(1)

    @field_validator("reddit_subreddit")
    @classmethod
    def valid_reddit_subreddit(cls, value: str | None) -> str | None:
        """Require one subreddit slug so owner input cannot add a Reddit API route segment."""
        if value is not None and re.fullmatch(r"[A-Za-z0-9_]{2,21}", value) is None:
            raise ValueError("Reddit scope must be one subreddit name")
        return value

    @field_validator("mastodon_instance_url")
    @classmethod
    def valid_mastodon_instance_url(cls, value: str | None) -> str | None:
        """Restrict a Mastodon instance to a credential-free HTTPS origin on the default port."""
        if value is None:
            return value
        parts = urlsplit(value)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("Mastodon instance port is invalid") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or port not in (None, 443)
        ):
            raise ValueError("Mastodon requires an HTTPS instance origin")
        return f"https://{parts.hostname.lower()}"

    @field_validator("mastodon_account")
    @classmethod
    def valid_mastodon_account(cls, value: str | None) -> str | None:
        """Accept one Mastodon acct identifier, excluding path and query syntax."""
        if (
            value is not None
            and re.fullmatch(r"@?[A-Za-z0-9_.-]{1,64}(?:@[A-Za-z0-9.-]{1,253})?", value) is None
        ):
            raise ValueError("Mastodon account scope is invalid")
        return value

    @field_validator("mastodon_hashtag")
    @classmethod
    def valid_mastodon_hashtag(cls, value: str | None) -> str | None:
        """Accept one hashtag token without its optional leading hash or path delimiters."""
        if value is not None and re.fullmatch(r"[A-Za-z0-9_]{1,100}", value) is None:
            raise ValueError("Mastodon hashtag scope is invalid")
        return value

    @field_validator("x_account")
    @classmethod
    def valid_x_account(cls, value: str | None) -> str | None:
        """Accept only a single X username for the fixed API route."""
        if value is not None and re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) is None:
            raise ValueError("X account scope is invalid")
        return value

    @field_validator("x_query", mode="before")
    @classmethod
    def clean_x_query(cls, value: object) -> object:
        """Strip control characters and fold spaces before a user search reaches the X API."""
        if not isinstance(value, str):
            return value
        return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())

    @field_validator("github_repository")
    @classmethod
    def valid_github_repository(cls, value: str | None) -> str | None:
        """Reject path-like repository values outside a single GitHub repository."""
        if value is not None and (value in {".", ".."} or value.lower().endswith(".git")):
            raise ValueError("GitHub repository must be a repository name without a path suffix")
        return value

    @field_validator("telegram_chat_ids")
    @classmethod
    def valid_telegram_chat_ids(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Require unique signed decimal channel identifiers within Bot API bounds."""
        if value is None:
            return value
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None for item in value
        ):
            raise ValueError("Telegram chat IDs must be unique signed decimal strings")
        return value

    @model_validator(mode="after")
    def validate_world_data_scope(self) -> "ConnectorConfig":
        """Keep finance, social, weather, workspace, Google, FHIR and Home Assistant scopes bounded.

        Runs the grouped checks in a fixed order, so the first violated rule determines the error.
        """
        _check_market_and_weather_scope(self)
        _check_workspace_app_scope(self)
        _check_social_scope(self)
        _check_local_service_scope(self)
        return self


def _require_unique_matching(values: Sequence[str] | None, pattern: str, message: str) -> None:
    """Raise ``ValueError(message)`` unless ``values`` is None or unique with every item matching ``pattern``."""
    if values is not None and (
        len(set(values)) != len(values)
        or any(re.fullmatch(pattern, value) is None for value in values)
    ):
        raise ValueError(message)


def _check_market_and_weather_scope(config: "ConnectorConfig") -> None:
    """Validate Alpha Vantage market symbols/currency, CoinGecko assets and Open-Meteo weather scope."""
    _require_unique_matching(
        config.market_symbols,
        r"[A-Z0-9.^_-]{1,20}",
        "Market symbols must be unique uppercase provider symbols",
    )
    if config.coingecko_assets is not None and len(
        {asset.id for asset in config.coingecko_assets}
    ) != len(config.coingecko_assets):
        raise ValueError("CoinGecko asset IDs must be unique")
    if config.market_symbols is not None and config.schedule_interval_minutes not in (None, 1440):
        raise ValueError("Alpha Vantage collection must be scheduled at most once per day")
    if (config.market_currency is None) != (config.market_exchange_timezone is None):
        raise ValueError("Market currency and exchange timezone must be configured together")
    weather_fields = (
        config.weather_latitude,
        config.weather_longitude,
        config.weather_timezone,
        config.weather_metrics,
    )
    if any(value is not None for value in weather_fields) and any(
        value is None for value in weather_fields
    ):
        raise ValueError("Weather coordinates, timezone, and metrics must be configured together")
    if config.weather_metrics is not None and (
        len(set(config.weather_metrics)) != len(config.weather_metrics)
        or not set(config.weather_metrics).issubset(
            {"temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"}
        )
    ):
        raise ValueError("Weather metric is outside the Open-Meteo bounded allowlist")


def _check_workspace_app_scope(config: "ConnectorConfig") -> None:
    """Validate Gmail, Google Calendar/Drive, Notion and Slack identifier lists (unique, bounded patterns)."""
    _require_unique_matching(
        config.google_mail_label_ids,
        r"[A-Za-z0-9_-]{1,225}",
        "Gmail labels must be unique provider label IDs",
    )
    _require_unique_matching(
        config.google_calendar_ids,
        r"[^\s\x00-\x1f]{1,256}",
        "Google Calendar IDs must be unique non-empty identifiers",
    )
    _require_unique_matching(
        config.google_drive_ids,
        r"[A-Za-z0-9_-]{5,200}",
        "Google Drive IDs must be unique provider file IDs",
    )
    _require_unique_matching(
        config.notion_page_ids,
        r"[A-Za-z0-9_-]{1,128}",
        "Notion page IDs must be unique provider identifiers",
    )
    _require_unique_matching(
        config.slack_channel_ids,
        r"[CG][A-Z0-9]{1,80}",
        "Slack scopes require unique channel identifiers",
    )


def _check_social_scope(config: "ConnectorConfig") -> None:
    """Require exactly one Mastodon account/hashtag (with instance URL) and one bounded X account/query."""
    if bool(config.mastodon_account) == bool(config.mastodon_hashtag) and (
        config.mastodon_account is not None or config.mastodon_hashtag is not None
    ):
        raise ValueError("Choose exactly one Mastodon account or hashtag")
    if (
        config.mastodon_account is not None or config.mastodon_hashtag is not None
    ) and not config.mastodon_instance_url:
        raise ValueError("Mastodon requires an instance URL for account or hashtag scope")
    if bool(config.x_account) == bool(config.x_query) and (
        config.x_account is not None or config.x_query is not None
    ):
        raise ValueError("Choose exactly one X account or search query")
    if config.x_query is not None and not 1 <= len(config.x_query) <= 512:
        raise ValueError("X search query is outside the supported bounds")


def _check_local_service_scope(config: "ConnectorConfig") -> None:
    """Validate FHIR / Home Assistant endpoints are credential-free HTTP(S) origins and their IDs are bounded."""
    for endpoint in (config.fhir_base_url, config.ha_base_url):
        if endpoint is None:
            continue
        parts = urlsplit(endpoint)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("Local service endpoint port is invalid") from exc
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("Local service endpoint must be a credential-free HTTP(S) origin")
    if config.fhir_patient_id is not None and (
        "/" in config.fhir_patient_id
        or re.fullmatch(r"[A-Za-z0-9.-]{1,128}", config.fhir_patient_id) is None
    ):
        raise ValueError("FHIR patient ID must be one identifier")
    _require_unique_matching(
        config.ha_entity_ids,
        r"[a-z0-9_]+\.[a-z0-9_]+",
        "Home Assistant entity IDs must be unique entity identifiers",
    )


@dataclass(frozen=True)
class ConnectorConfigurationSnapshot:
    """Expose persisted connector settings and activation state without secret values."""

    workspace_id: UUID
    source_id: UUID
    source_type: str
    provider: str | None
    source_generation: int
    configuration: dict[str, object]
    expected_revision: int
    auth_method: str
    auth_header_name: str | None
    desired_enabled: bool
    activation_state: str
    activation_error_code: str | None
    provider_credential_configured: bool
    provider_credential_state: str | None
    execution_backend: str = "n8n"
    transition_phase: str = "idle"
    target_backend: str | None = None


@dataclass(frozen=True)
class ProviderScopeSnapshot:
    """Expose only a generation-fenced digest of supported provider scope."""

    workspace_id: UUID
    source_id: UUID
    source_generation: int
    provider_id: str
    discriminator: str


class ConnectorRecord(BaseModel):
    """Validate one normalized record returned by a connector collector."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=1_000_000)
    observed_at: datetime
    version: str | None = Field(default=None, max_length=255)
    metadata: dict[str, object] = Field(default_factory=dict)


class ProviderCollectionPage(BaseModel):
    """Bound provider mapper output to detached ingestion records and coverage state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[IngestionRecord, ...] = Field(max_length=500)
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
    next_eligible_at: datetime | None = None
    credential_operation_id: UUID | None = None

    @field_validator("next_eligible_at")
    @classmethod
    def aware_provider_deadline(cls, value: datetime | None) -> datetime | None:
        """Normalize retry deadlines to UTC so shared cooldown storage gets an instant."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_eligible_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


class ConnectorReceipt(BaseModel):
    """Validate an acknowledged nonempty batch and its generation/revision fence."""

    model_config = ConfigDict(extra="forbid")

    cursor_before: str | None = Field(default=None, max_length=4096)
    cursor_after: str | None = Field(default=None, max_length=4096)
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int | None = Field(default=None, ge=1)
    records: list[ConnectorRecord] = Field(min_length=1, max_length=500)


class ConnectorPreview(BaseModel):
    """Validate a preview batch, which may contain no records."""

    model_config = ConfigDict(extra="forbid")

    cursor_before: str | None = Field(default=None, max_length=4096)
    cursor_after: str | None = Field(default=None, max_length=4096)
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    records: list[ConnectorRecord] = Field(max_length=500)


class CollectionFence(BaseModel):
    """Bind collection work to one source generation and connector revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    # Every current n8n template sends it; a stale or late workflow carrying an older value is rejected.
    backend_revision: int | None = Field(default=None, ge=1)


class ConnectorConfigurationRequest(BaseModel):
    """Validate a revision-fenced connector configuration replacement."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    configuration: ConnectorConfig


class RSSRequest(BaseModel):
    """Validate HTTP(S) feed URL shape and an optional bounded cursor.

    ``HttpUrl`` does not check DNS address visibility; collection-time
    ``validate_public_url`` performs the public-address check before transport.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    cursor: str | None = Field(default=None, max_length=4096)


class CrawlRequest(BaseModel):
    """Validate source/revision-fenced crawl settings and bounded traversal limits."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int | None = Field(default=None, ge=1)
    url: HttpUrl
    mode: str = Field(default="http", pattern="^(http|playwright)$")
    max_pages: int = Field(default=10, ge=1, le=10)
    max_depth: int = Field(default=2, ge=0, le=2)
    timeout_seconds: int = Field(default=60, ge=1, le=60)


class CrawlResult(BaseModel):
    """Return the durable ingestion run ID created by a crawl submission."""

    run_id: UUID


class NoChangeRequest(CollectionFence):
    """Represent a collection acknowledgement that advances no cursor or records."""
