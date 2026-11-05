"""Connector DTOs, configuration model and snapshots exposed through ``public``.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeGuard
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from core.workspaces.schemas import AccessFence
from modules.connectors.backends import (
    is_native_provider as _is_native_provider,
)
from modules.connectors.provider_options import (
    ISO_COUNTRY_PATTERN,
    MAX_COINS,
    MAX_JOB_KEYWORDS,
    MAX_VN_SYMBOLS,
    CoinId,
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
    clean_query,
)
from modules.connectors.sections import MAX_SECTIONS
from modules.ingestion.public import IngestionRecord, TelegramRawDelivery
from modules.sources.public import SourceFence

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"


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


class ConnectorConfig(BaseModel):
    """Validate bounded source scopes, per-provider mappings, timezone, and schedule settings.

    Social, Google, FHIR, Home Assistant, Notion, and Slack selections are checked against the
    provider-specific field bounds before they can be persisted or activated.
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
    arxiv_category: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9.-]{0,63}$")
    huggingface_author: str | None = Field(default=None, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
    bluesky_actor: str | None = Field(default=None, min_length=3, max_length=253)
    reddit_subreddit: str | None = Field(default=None, min_length=2, max_length=21)
    mastodon_instance_url: str | None = Field(default=None, min_length=8, max_length=512)
    mastodon_account: str | None = Field(default=None, min_length=1, max_length=320)
    mastodon_hashtag: str | None = Field(default=None, min_length=1, max_length=100)
    x_account: str | None = Field(default=None, min_length=1, max_length=15)
    x_query: str | None = Field(default=None, min_length=1, max_length=512)
    github_owner: str | None = Field(default=None, min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    github_repository: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    include_issues: bool = False
    include_pulls: bool = False
    include_commits: bool = False
    include_releases: bool = False
    github_history_days: StrictInt = Field(default=90, ge=1, le=365)
    telegram_chat_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    market_symbols: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=5)
    coingecko_assets: tuple["CoinGeckoAsset", ...] | None = Field(default=None, min_length=1, max_length=20)
    market_currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
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
        default=None, max_length=254, pattern=r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
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
    feed_sections: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=MAX_SECTIONS)
    # Wave D per-type options (enums/bounded values; the registry allows each only on its own provider).
    hn_list: HnList | None = None
    gdelt_query: str | None = Field(default=None, min_length=1, max_length=100)
    gdelt_timespan: GdeltTimespan | None = None
    gdelt_source_lang: GdeltLang | None = None
    gdelt_source_country: GdeltCountry | None = None
    usgs_min_magnitude: UsgsMagnitude | None = None
    usgs_period: UsgsPeriod | None = None
    min_magnitude: float | None = Field(default=None, ge=2.0, le=8.0)
    eonet_categories: tuple[EonetCategory, ...] | None = Field(default=None, min_length=1, max_length=13)
    eonet_status: EonetStatus | None = None
    eonet_days: StrictInt | None = Field(default=None, ge=1, le=30)
    gdacs_event_types: tuple[GdacsType, ...] | None = Field(default=None, min_length=1, max_length=6)
    gdacs_alert_levels: tuple[GdacsAlert, ...] | None = Field(default=None, min_length=1, max_length=3)
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
    vn_symbols: tuple[VnSymbol, ...] | None = Field(default=None, min_length=1, max_length=MAX_VN_SYMBOLS)
    job_keywords: tuple[JobKeyword, ...] | None = Field(default=None, min_length=1, max_length=MAX_JOB_KEYWORDS)
    job_cities: tuple[JobCity, ...] | None = Field(default=None, min_length=1, max_length=3)
    job_levels: tuple[JobLevel, ...] | None = Field(default=None, min_length=1, max_length=5)
    job_min_salary_usd: StrictInt | None = Field(default=None, ge=0, le=20000)
    coin_ids: tuple[CoinId, ...] | None = Field(default=None, min_length=1, max_length=MAX_COINS)
    kp_min: float | None = Field(default=None, ge=0, le=9)
    advisory_min_level: StrictInt | None = Field(default=None, ge=1, le=4)

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
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment or port not in (None, 443)):
            raise ValueError("Mastodon requires an HTTPS instance origin")
        return f"https://{parts.hostname.lower()}"

    @field_validator("mastodon_account")
    @classmethod
    def valid_mastodon_account(cls, value: str | None) -> str | None:
        """Accept one Mastodon acct identifier, excluding path and query syntax."""
        if value is not None and re.fullmatch(r"@?[A-Za-z0-9_.-]{1,64}(?:@[A-Za-z0-9.-]{1,253})?", value) is None:
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
        len(set(values)) != len(values) or any(re.fullmatch(pattern, value) is None for value in values)
    ):
        raise ValueError(message)


def _check_market_and_weather_scope(config: "ConnectorConfig") -> None:
    """Validate Alpha Vantage market symbols/currency, CoinGecko assets and Open-Meteo weather scope."""
    _require_unique_matching(
        config.market_symbols, r"[A-Z0-9.^_-]{1,20}", "Market symbols must be unique uppercase provider symbols",
    )
    if config.coingecko_assets is not None and len({asset.id for asset in config.coingecko_assets}) != len(config.coingecko_assets):
        raise ValueError("CoinGecko asset IDs must be unique")
    if config.market_symbols is not None and config.schedule_interval_minutes not in (None, 1440):
        raise ValueError("Alpha Vantage collection must be scheduled at most once per day")
    if (config.market_currency is None) != (config.market_exchange_timezone is None):
        raise ValueError("Market currency and exchange timezone must be configured together")
    weather_fields = (config.weather_latitude, config.weather_longitude, config.weather_timezone, config.weather_metrics)
    if any(value is not None for value in weather_fields) and any(value is None for value in weather_fields):
        raise ValueError("Weather coordinates, timezone, and metrics must be configured together")
    if config.weather_metrics is not None and (
        len(set(config.weather_metrics)) != len(config.weather_metrics)
        or not set(config.weather_metrics).issubset({"temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m"})
    ):
        raise ValueError("Weather metric is outside the Open-Meteo bounded allowlist")


def _check_workspace_app_scope(config: "ConnectorConfig") -> None:
    """Validate Gmail, Google Calendar/Drive, Notion and Slack identifier lists (unique, bounded patterns)."""
    _require_unique_matching(
        config.google_mail_label_ids, r"[A-Za-z0-9_-]{1,225}", "Gmail labels must be unique provider label IDs",
    )
    _require_unique_matching(
        config.google_calendar_ids, r"[^\s\x00-\x1f]{1,256}", "Google Calendar IDs must be unique non-empty identifiers",
    )
    _require_unique_matching(
        config.google_drive_ids, r"[A-Za-z0-9_-]{5,200}", "Google Drive IDs must be unique provider file IDs",
    )
    _require_unique_matching(
        config.notion_page_ids, r"[A-Za-z0-9_-]{1,128}", "Notion page IDs must be unique provider identifiers",
    )
    _require_unique_matching(
        config.slack_channel_ids, r"[CG][A-Z0-9]{1,80}", "Slack scopes require unique channel identifiers",
    )


def _check_social_scope(config: "ConnectorConfig") -> None:
    """Require exactly one Mastodon account/hashtag (with instance URL) and one bounded X account/query."""
    if (bool(config.mastodon_account) == bool(config.mastodon_hashtag)
            and (config.mastodon_account is not None or config.mastodon_hashtag is not None)):
        raise ValueError("Choose exactly one Mastodon account or hashtag")
    if (config.mastodon_account is not None or config.mastodon_hashtag is not None) and not config.mastodon_instance_url:
        raise ValueError("Mastodon requires an instance URL for account or hashtag scope")
    if bool(config.x_account) == bool(config.x_query) and (config.x_account is not None or config.x_query is not None):
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
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("Local service endpoint must be a credential-free HTTP(S) origin")
    if config.fhir_patient_id is not None and (
        "/" in config.fhir_patient_id or re.fullmatch(r"[A-Za-z0-9.-]{1,128}", config.fhir_patient_id) is None
    ):
        raise ValueError("FHIR patient ID must be one identifier")
    _require_unique_matching(
        config.ha_entity_ids, r"[a-z0-9_]+\.[a-z0-9_]+", "Home Assistant entity IDs must be unique entity identifiers",
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
