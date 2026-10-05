from datetime import UTC, datetime, timedelta
import base64
import hashlib
from hashlib import sha256
import json
from ipaddress import ip_address
from socket import getaddrinfo
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import asyncio
from dataclasses import dataclass
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StrictInt, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from modules.sources.schemas import ConnectorSource, SourceFence
from modules.connectors.models import (
    AgentBrowserGrant, ConnectorProvisioning, GithubOAuthGrant, GithubWebhookCapacity,
    GithubWebhookDelivery, GithubWebhookOutbox, GithubSourceHint,
)
from modules.ingestion.schemas import IngestionRecord, TelegramRawDelivery


async def observability_queue_summary(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Return connector-owned provisioning counts without workflow or credential data."""
    counts = dict((await session.execute(
        select(ConnectorProvisioning.state, func.count()).group_by(ConnectorProvisioning.state)
    )).all())
    return {"connector_provisioning": counts}

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_OVERLAP = timedelta(days=1)
NATIVE_PROVIDERS = frozenset({"youtube", "arxiv", "huggingface", "github_releases", "github", "telegram"})
PROVIDER_SOURCE_TYPES = {"youtube": "rss", "arxiv": "rss", "huggingface": "api", "github_releases": "api", "github": "api", "telegram": "api"}


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
    source_id: UUID
    operation_id: UUID
    source_generation: int
    configuration_revision: int
    verified_bot_id: str | None
    bound_bot_id: str | None
    encrypted_token: str | None
    state: str
    validated_at: datetime | None


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


def is_native_provider(provider: str | None) -> bool:
    """Identify registered native providers that must bypass generic raw collection."""
    return provider in NATIVE_PROVIDERS


class ConnectorConfig(BaseModel):
    """Validate bounded source, REST mapping, timezone, and schedule settings."""
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
    github_owner: str | None = Field(default=None, min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    github_repository: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    include_issues: bool = False
    include_pulls: bool = False
    include_commits: bool = False
    include_releases: bool = False
    github_history_days: StrictInt = Field(default=90, ge=1, le=365)
    telegram_chat_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=100)
    history_mode: Literal["returned_snapshot", "pending_updates"] | None = None

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


@dataclass(frozen=True)
class ConnectorConfigurationSnapshot:
    """Expose persisted connector settings and activation state without secret values."""
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


@dataclass(frozen=True)
class ProviderScopeSnapshot:
    """Expose only a generation-fenced digest of supported provider scope."""
    source_id: UUID
    source_generation: int
    provider_id: str
    discriminator: str


async def get_current_provider_scope(
    session: AsyncSession, source_id: UUID, expected_source_generation: int,
) -> ProviderScopeSnapshot | None:
    """Hash validated non-secret scope fields for supported active providers only.

    The result contains no connector configuration values. Unsupported providers,
    inactive sources, invalid config, and stale generations fail closed as None.
    """
    from modules.connectors.catalog import get_catalog_entry
    from modules.sources import public as sources

    source = await sources.get_connector_source(session, source_id)
    if source is None or source.status != "active" or source.generation != expected_source_generation:
        return None
    provider_id = {"rss": "rss", "web": "web", "api": "rest"}.get(source.type)
    entry = get_catalog_entry(provider_id) if provider_id else None
    if entry is None or entry.availability != "available":
        return None
    try:
        configuration = ConnectorConfig.model_validate(source.configuration).model_dump(
            mode="json", exclude_none=True,
        )
    except Exception:
        return None
    values = {name: configuration[name] for name in entry.scope_fields if name in configuration}
    if len(values) != len(entry.scope_fields):
        return None
    encoded = json.dumps([provider_id, values], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ProviderScopeSnapshot(
        source_id=source.id, source_generation=source.generation,
        provider_id=provider_id, discriminator=hashlib.sha256(encoded.encode()).hexdigest(),
    )


@dataclass(frozen=True)
class AgentBrowserScope:
    """Bind browser reads to current source and connector revisions and one exact HTTPS path."""

    source_id: UUID
    source_generation: int
    connector_revision: int
    grant_revision: int
    scope_hash: str
    origin: str
    path_prefix: str
    local_only: bool
    enabled: bool


@dataclass(frozen=True)
class AgentBrowserScopeRead(AgentBrowserScope):
    """Expose only the owner-approved source scope and its revision fences."""


class AgentBrowserGrantPatch(BaseModel):
    """Allow only an explicit enable change fenced to the current source configuration."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: StrictBool
    expected_source_generation: StrictInt = Field(ge=1)
    expected_connector_revision: StrictInt = Field(ge=1)


def _agent_browser_scope_url(value: object) -> tuple[str, str]:
    """Derive a normalized HTTPS origin and path prefix from configured source URL only."""
    from urllib.parse import unquote, urlsplit

    if not isinstance(value, str):
        raise ValueError("Web source URL is unavailable")
    parsed = urlsplit(value)
    path = parsed.path or "/"
    decoded_path = unquote(path)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or (parsed.port is not None and parsed.port != 443)
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or "%2f" in path.lower()
        or "%5c" in path.lower()
        or "\\" in decoded_path
    ):
        raise ValueError("Browser grant requires an unambiguous credential-free HTTPS source URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ip_address(host)
        if getattr(address, "scope_id", None) is not None:
            raise ValueError("Scoped IP source URLs are not allowed")
        rendered_host = f"[{host}]" if address.version == 6 else host
    except ValueError as exc:
        if "Scoped IP" in str(exc):
            raise
        rendered_host = host
    return f"https://{rendered_host}", path.rstrip("/") or "/"


def agent_browser_target_in_scope(scope: AgentBrowserScope, value: str | None) -> bool:
    """Match a credential-free HTTPS target to one exact origin and path-segment grant."""
    from urllib.parse import unquote, urlsplit

    if not value or len(value.encode("utf-8")) > 2048 or "?" in value or "#" in value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        raw_host = parsed.hostname
        if raw_host is None:
            return False
        host = raw_host.encode("idna").decode("ascii").lower()
        try:
            address = ip_address(host)
            if getattr(address, "scope_id", None) is not None:
                return False
            rendered = f"[{host}]" if address.version == 6 else host
        except ValueError:
            rendered = host
    except (UnicodeError, ValueError):
        return False
    path = unquote(parsed.path or "/")
    prefix = unquote(scope.path_prefix or "/")
    return bool(
        parsed.scheme == "https" and parsed.username is None and parsed.password is None
        and parsed.query == "" and parsed.fragment == ""
        and f"https://{rendered}" == scope.origin
        and (port is None or port == 443)
        and not any(segment in {".", ".."} for segment in path.split("/"))
        and "%2f" not in parsed.path.lower() and "%5c" not in parsed.path.lower()
        and "%25" not in parsed.path.lower()
        and "\\" not in path
        and (path == prefix or prefix == "/" or path.startswith(prefix.rstrip("/") + "/"))
    )


def _scope_hash(source_id: UUID, generation: int, revision: int, origin: str, path: str) -> str:
    """Hash canonical source identity and bounded scope fields for stale-grant detection."""
    value = json.dumps(
        [str(source_id), generation, revision, origin, path],
        separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(value.encode("ascii")).hexdigest()


async def resolve_agent_browser_scope(
    session: AsyncSession, owner_id: int, source_id: UUID
) -> AgentBrowserScope | None:
    """Resolve an enabled grant only while the active web source and connector revision match."""
    from modules.sources import public as sources

    if owner_id != 1:
        return None
    source = await sources.get_connector_source(session, source_id)
    if source is None or source.status != "active" or source.type != "web":
        return None
    configuration = await get_connector_configuration(session, source_id)
    row = await session.get(AgentBrowserGrant, source_id)
    if configuration is None or row is None:
        return None
    try:
        origin, path_prefix = _agent_browser_scope_url(configuration.configuration.get("url"))
    except (TypeError, ValueError):
        return None
    scope_hash = _scope_hash(
        source_id, source.generation, configuration.expected_revision, origin, path_prefix
    )
    if (
        row.owner_id != owner_id
        or row.source_generation != source.generation
        or row.connector_revision != configuration.expected_revision
        or row.scope_hash != scope_hash
        or row.origin != origin
        or row.path_prefix != path_prefix
    ):
        return None
    return AgentBrowserScope(
        source_id, source.generation, configuration.expected_revision,
        row.grant_revision, scope_hash, origin, path_prefix,
        source.local_only, row.enabled and not source.local_only,
    )


async def invalidate_agent_browser_grant_in_uow(
    session: AsyncSession, source_id: UUID
) -> None:
    """Disable and revision-bump an existing browser grant in its source lifecycle transaction."""
    row = await session.get(AgentBrowserGrant, source_id, with_for_update=True)
    if row is None:
        return
    row.enabled = False
    row.grant_revision += 1
    await session.flush()


async def update_agent_browser_grant_in_uow(
    session: AsyncSession, owner_id: int, source_id: UUID,
    expected_revision: int, grant: AgentBrowserGrantPatch,
) -> AgentBrowserScopeRead:
    """Update explicit browser opt-in for the current configured web source; caller commits."""
    from modules.sources import public as sources

    if owner_id != 1:
        raise PermissionError("Browser grant owner is unavailable")
    source = await sources.lock_source(session, source_id)
    source_view = await sources.get_connector_source(session, source_id)
    configuration = await get_connector_configuration(session, source_id)
    if (
        source is None or source_view is None or source.status != "active"
        or source_view.type != "web" or configuration is None
    ):
        raise LookupError("Active configured web source is unavailable")
    if (
        type(expected_revision) is not int or expected_revision < 0
        or grant.expected_source_generation != source.generation
        or grant.expected_connector_revision != configuration.expected_revision
    ):
        raise ValueError("Browser grant source or connector revision conflict")
    origin, path_prefix = _agent_browser_scope_url(configuration.configuration.get("url"))
    scope_hash = _scope_hash(
        source_id, source.generation, configuration.expected_revision, origin, path_prefix
    )
    row = await session.get(AgentBrowserGrant, source_id, with_for_update=True)
    current_revision = row.grant_revision if row is not None else 0
    if current_revision != expected_revision:
        raise ValueError("Browser grant revision conflict")
    if row is None:
        row = AgentBrowserGrant(
            source_id=source_id, owner_id=owner_id, source_generation=source.generation,
            connector_revision=configuration.expected_revision, grant_revision=1,
            scope_hash=scope_hash, origin=origin, path_prefix=path_prefix,
            local_only=source.local_only, enabled=grant.enabled and not source.local_only,
        )
        session.add(row)
    else:
        row.source_generation = source.generation
        row.connector_revision = configuration.expected_revision
        row.grant_revision += 1
        row.scope_hash = scope_hash
        row.origin = origin
        row.path_prefix = path_prefix
        row.local_only = source.local_only
        row.enabled = grant.enabled and not source.local_only
    await session.flush()
    return AgentBrowserScopeRead(
        source_id, source.generation, configuration.expected_revision,
        row.grant_revision, scope_hash, origin, path_prefix,
        source.local_only, row.enabled,
    )


async def get_connector_configuration(
    session: AsyncSession, source_id: UUID
) -> ConnectorConfigurationSnapshot | None:
    """Return a coherent owner-safe configuration view under source-first locks."""
    from modules.sources import public as sources
    from modules.connectors import provisioning

    source_fence, row, credentials = await provisioning.lock_connector(
        session, source_id, ("provider",)
    )
    if source_fence is None:
        return None
    source = await sources.get_connector_source(session, source_id)
    if source is None:
        return None
    if source.generation != source_fence.generation:
        raise RuntimeError("Locked source snapshot generation mismatch")
    provider_credential = credentials.get("provider")
    native_credential = None
    if source.provider == "telegram" and row is not None:
        native_credential = await get_native_credential_snapshot(
            session,
            source_id,
            source_generation=source.generation,
            connector_revision=row.desired_revision,
        )
    configuration = ConnectorConfig.model_validate(source.configuration).model_dump(
        mode="json", exclude_none=True
    )
    if source.provider in NATIVE_PROVIDERS:
        scope_fields = {
            "youtube": {"youtube_channel_id"}, "arxiv": {"arxiv_category"},
            "huggingface": {"huggingface_author"},
            "github_releases": {"github_owner", "github_repository"},
            "github": {"github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "github_history_days"},
            "telegram": {"telegram_chat_ids"},
        }[source.provider]
        common = {"timezone", "schedule_interval_minutes", "timeout_seconds", "history_mode"}
        configuration = {
            key: value for key, value in configuration.items()
            if key in scope_fields | common
        }
        configuration.setdefault("history_mode", "pending_updates" if source.provider == "telegram" else "returned_snapshot")
        configuration.setdefault("timezone", DEFAULT_TIMEZONE)
        configuration.setdefault("schedule_interval_minutes", default_schedule_interval_minutes(source.type))
        if "timeout_seconds" not in source.configuration:
            configuration["timeout_seconds"] = 30
    if "schedule_interval_minutes" not in configuration:
        configuration["schedule_interval_minutes"] = default_schedule_interval_minutes(
            source.type
        )
    desired = row.desired_configuration if row is not None else {}
    return ConnectorConfigurationSnapshot(
        source_id=source.id,
        source_type=source.type,
        provider=source.provider,
        source_generation=source.generation,
        configuration=configuration,
        expected_revision=row.desired_revision if row is not None else 0,
        auth_method=(
            "telegram_bot_token" if source.provider == "telegram"
            else "none" if source.provider in NATIVE_PROVIDERS
            else str(desired.get("auth_method", "none"))
        ),
        auth_header_name=(
            str(desired["auth_header_name"])
            if desired.get("auth_header_name") is not None
            else None
        ),
        desired_enabled=bool(row and row.desired_enabled),
        activation_state=row.state if row is not None else "saved_not_active",
        activation_error_code=row.error_code if row is not None else None,
        provider_credential_configured=(
            bool(native_credential is not None and native_credential.state == "ready"
                 and native_credential.encrypted_token and native_credential.verified_bot_id
                 and native_credential.validated_at
                 and native_credential.source_generation == source.generation
                 and row is not None
                 and native_credential.configuration_revision == row.desired_revision)
            if source.provider == "telegram"
            else bool(provider_credential is not None and provider_credential.state == "ready"
                      and provider_credential.credential_id)
        ),
        provider_credential_state=(native_credential.state if source.provider == "telegram" and native_credential is not None
                                   else provider_credential.state if provider_credential is not None else None),
    )


def default_schedule_interval_minutes(source_type: str) -> int:
    """Return the default polling cadence for RSS versus other source types."""
    return 15 if source_type == "rss" else 30


def serialize_source_configuration(source: ConnectorSource, config: ConnectorConfig) -> dict[str, object]:
    """Serialize generic settings unchanged and native scopes with a reusable 30-second default.

    Pydantic's generic timeout default remains 60 seconds. For native adapters,
    an omitted timeout selects 30 seconds; an explicitly supplied value above
    30 is rejected so GET/PUT round-trips cannot accidentally reject the default.
    """
    values = config.model_dump(mode="json", exclude_none=True)
    if source.provider not in NATIVE_PROVIDERS:
        return values
    scope_fields = {
        "youtube": {"youtube_channel_id"}, "arxiv": {"arxiv_category"},
        "huggingface": {"huggingface_author"},
        "github_releases": {"github_owner", "github_repository"},
        "github": {"github_owner", "github_repository", "include_issues", "include_pulls", "include_commits", "include_releases", "github_history_days"},
        "telegram": {"telegram_chat_ids"},
    }[source.provider]
    common = {"timezone", "schedule_interval_minutes", "timeout_seconds", "history_mode"}
    supplied = config.model_fields_set
    if supplied - scope_fields - common:
        raise ValueError("Native provider configuration contains unsupported fields")
    expected_history = "pending_updates" if source.provider == "telegram" else "returned_snapshot"
    if values.get("history_mode", expected_history) != expected_history:
        raise ValueError("Provider history mode does not match its collection contract")
    timeout_seconds = config.timeout_seconds if "timeout_seconds" in supplied else 30
    if timeout_seconds > 30:
        raise ValueError("Native provider request timeout cannot exceed 30 seconds")
    serialized = {key: value for key, value in values.items() if key in scope_fields | common}
    serialized["history_mode"] = expected_history
    serialized.setdefault("timezone", DEFAULT_TIMEZONE)
    serialized.setdefault("schedule_interval_minutes", default_schedule_interval_minutes(source.type))
    serialized["timeout_seconds"] = timeout_seconds
    return serialized


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

    @field_validator("next_eligible_at")
    @classmethod
    def aware_provider_deadline(cls, value: datetime | None) -> datetime | None:
        """Normalize retry deadlines to UTC so shared cooldown storage gets an instant."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_eligible_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


async def get_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
) -> NativeCredentialSnapshot | None:
    """Expose a detached native credential projection to the ingestion owner only."""
    from modules.connectors import provisioning

    return await provisioning.get_native_credential_snapshot(
        session,
        source_id,
        source_generation=source_generation,
        connector_revision=connector_revision,
    )


class ConnectorReceipt(BaseModel):
    """Validate an acknowledged nonempty batch and its generation/revision fence."""
    model_config = ConfigDict(extra="forbid")

    cursor_before: str | None = Field(default=None, max_length=4096)
    cursor_after: str | None = Field(default=None, max_length=4096)
    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
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


class ConnectorConfigurationRequest(BaseModel):
    """Validate a revision-fenced connector configuration replacement."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    configuration: ConnectorConfig


async def collection_allowed(
    session: AsyncSession,
    source_id: UUID,
    source_status: str,
    source_generation: int,
    fence: CollectionFence,
    *,
    lock: bool = False,
) -> bool:
    """Check source and connector revisions, optionally locking the provisioning row."""
    from modules.connectors import provisioning

    source = ConnectorSource(
        id=source_id,
        type="api",
        status=source_status,
        generation=source_generation,
        configuration={},
    )
    return await provisioning.require_collection_fence(
        session, source, fence.source_generation, fence.connector_revision, lock=lock
    )


async def require_collection_fence(
    session: AsyncSession,
    source: ConnectorSource,
    fence: CollectionFence,
    *,
    lock: bool = False,
) -> bool:
    """Enforce the supplied collection fence through connector provisioning state."""
    from modules.connectors import provisioning

    return await provisioning.require_collection_fence(
        session,
        source,
        fence.source_generation,
        fence.connector_revision,
        lock=lock,
    )


async def get_github_binding_fence(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    lock: bool = False,
) -> "GitHubBindingFence | None":
    """Return the current nonsecret GitHub collection identity after source and provisioning fences.

    A ready unexpired grant must match the active source generation and desired connector revision.
    Legacy grants without installation or App IDs can produce polling fences from their verified
    repository and current OAuth state; nullable identities never authorize webhook matching. Lock
    mode follows source, provisioning, then GitHub grant order.
    """
    from modules.connectors.github.schemas import GitHubBindingFence, project_github_source_config
    from modules.connectors.github.sync import github_scope_digest
    from modules.connectors.models import GithubOAuthGrant
    from modules.sources import public as sources

    if lock and await sources.lock_source(session, source_id) is None:
        return None
    source = await sources.get_connector_source(session, source_id)
    if source is None or source.provider != "github" or source.status != "active":
        return None
    if source.generation != source_generation or not await require_collection_fence(
        session, source, CollectionFence(
            source_generation=source_generation, connector_revision=connector_revision,
        ), lock=lock,
    ):
        return None
    query = select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
    if lock:
        query = query.with_for_update()
    grant = await session.scalar(query)
    now = datetime.now(UTC)
    if (
        grant is None or grant.state != "ready" or grant.encrypted_tokens is None
        or grant.expires_at is None or grant.expires_at <= now
        or grant.source_generation != source_generation
        or grant.configuration_revision != connector_revision
    ):
        return None
    config = project_github_source_config(source.configuration)
    resources = tuple(name for name, enabled in (
        ("issue", config.include_issues), ("pull", config.include_pulls),
        ("commit", config.include_commits), ("release", config.include_releases),
    ) if enabled)
    scope = github_scope_digest(
        str(source.id), source_generation, connector_revision, grant.repository_id,
        grant.installation_id, grant.app_id, resources, config.github_history_days,
    )
    try:
        return GitHubBindingFence(
            source_id=source.id, source_generation=source_generation,
            connector_revision=connector_revision, grant_operation_id=grant.operation_id,
            token_revision=grant.token_revision, repository_id=grant.repository_id,
            installation_id=grant.installation_id, app_id=grant.app_id,
            binding_revision=grant.binding_revision, resource_scope=resources,
            history_days=config.github_history_days, scope_sha256=scope,
        )
    except (TypeError, ValueError):
        return None


async def validate_github_collection_segment(
    session: AsyncSession,
    *,
    source: ConnectorSource,
    reserved_cursor_before: str | None,
    proof: "GitHubSegmentProof",
) -> "GitHubValidatedSegment":
    """Revalidate the current GitHub grant and derive records plus cursor from its raw page proof.

    This owner boundary performs no network calls or commits. The ingestion receipt transaction
    remains responsible for atomically persisting the recomputed cursor with its accepted batch.
    """
    from fastapi import HTTPException
    from modules.connectors.github.schemas import GitHubSegmentProof, project_github_source_config
    from modules.connectors.github.sync import GitHubValidatedSegment, validate_github_segment

    if source.provider != "github":
        raise HTTPException(status_code=422, detail="GitHub segment is not valid for this provider")
    try:
        proof = GitHubSegmentProof.model_validate(proof.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="GitHub segment proof is malformed") from exc
    fence = await get_github_binding_fence(
        session, source.id, source_generation=source.generation,
        connector_revision=proof.fence.connector_revision, lock=True,
    )
    if fence is None:
        raise HTTPException(status_code=409, detail="GitHub grant is unavailable or requires reconnection")
    try:
        return validate_github_segment(
            fence, project_github_source_config(source.configuration),
            reserved_cursor_before, proof, collected_at=proof.collected_at,
        )
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("github_") else "github_segment_invalid"
        status = 409 if "stale" in code or "scope_exhausted" in code else 422
        raise HTTPException(status_code=status, detail=code) from exc


async def reset_github_collection_cursor(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    expected_scope_sha256: str,
) -> None:
    """Reset GitHub progress only for the currently locked binding and exact reviewed scope hash.

    Lock order is source, provisioning, GitHub grant, then ingestion state. Ingestion rejects live
    collection leases or nonterminal runs and publishes the cursor clear through its receipt owner.
    """
    import re
    from fastapi import HTTPException
    from modules.ingestion import public as ingestion
    from modules.connectors.models import GithubSyncReset
    from modules.sources import public as sources

    if re.fullmatch(r"[0-9a-f]{64}", expected_scope_sha256) is None:
        raise HTTPException(status_code=422, detail="GitHub scope review is invalid")
    if await sources.lock_source(session, source_id) is None:
        raise HTTPException(status_code=404, detail="Source not found")
    fence = await get_github_binding_fence(
        session, source_id, source_generation=source_generation,
        connector_revision=connector_revision, lock=True,
    )
    if fence is None:
        raise HTTPException(status_code=409, detail="GitHub binding is unavailable")
    if fence.scope_sha256 != expected_scope_sha256:
        raise HTTPException(status_code=409, detail="GitHub scope changed; review current sync status")
    session.add(GithubSyncReset(
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, scope_sha256=fence.scope_sha256,
    ))
    await ingestion.reset_native_collection_cursor(session, source_id)


class GitHubEventBinding(BaseModel):
    """Project one current or historically fenced App binding without its encrypted token."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_status: str
    source_generation: int
    connector_revision: int
    grant_state: str
    grant_expires_at: datetime | None
    repository_id: str
    installation_id: str | None
    app_id: str
    binding_revision: int
    resource_scope: tuple[str, ...]


class GitHubEventBindingPage(BaseModel):
    """Return one bounded keyset page of safe webhook binding identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[GitHubEventBinding, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=512)


class _GitHubFanoutBinding(BaseModel):
    """Persist only the bounded, detached, nonsecret binding fields frozen for one fanout page."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_status: Literal["active", "paused", "archived"]
    source_generation: StrictInt = Field(gt=0)
    connector_revision: StrictInt = Field(ge=0)
    grant_state: Literal["ready", "refreshing", "reconciliation_required", "revoked"]
    grant_expires_at: datetime | None
    repository_id: str = Field(pattern=r"^[0-9]{1,20}$")
    installation_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    app_id: str = Field(pattern=r"^[0-9]{1,20}$")
    binding_revision: StrictInt = Field(gt=0)
    resource_scope: tuple[Literal["issue", "pull", "commit", "release"], ...] = Field(max_length=4)

    @field_validator("grant_expires_at")
    @classmethod
    def aware_grant_expiry(cls, value: datetime | None) -> datetime | None:
        """Require persisted grant expiry to be an aware UTC instant when present."""
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Grant expiry must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def unique_resource_scope(self) -> "_GitHubFanoutBinding":
        """Reject repeated resource names in a frozen authority snapshot."""
        if len(self.resource_scope) != len(set(self.resource_scope)):
            raise ValueError("GitHub fanout resource scope must be unique")
        return self


class _GitHubFanoutAdmission(BaseModel):
    """Remember one delivery/source/distinct-target result within the current frozen page."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: "GitHubHintEnqueueResult"


class _GitHubFanoutPage(BaseModel):
    """Bound one restart-safe binding page and all first-admission results attached to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cursor_before: str | None = Field(default=None, max_length=512)
    cursor_after: str | None = Field(default=None, max_length=512)
    bindings: tuple[_GitHubFanoutBinding, ...] = Field(max_length=50)
    admissions: tuple[_GitHubFanoutAdmission, ...] = Field(max_length=5000)

    @model_validator(mode="after")
    def validate_fanout_bounds_and_uniqueness(self) -> "_GitHubFanoutPage":
        """Reject duplicate frozen identities and keep canonical persisted state under two MiB."""
        binding_ids = [item.source_id for item in self.bindings]
        admission_ids = [(item.source_id, item.target_sha256) for item in self.admissions]
        if len(binding_ids) != len(set(binding_ids)) or len(admission_ids) != len(set(admission_ids)):
            raise ValueError("GitHub fanout identities must be unique")
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("GitHub fanout page exceeds its byte bound")
        return self


class GitHubHintEnqueueResult(BaseModel):
    """Report the durable source target result without treating dispatch as ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    hint_id: UUID | None = None
    dirty_revision: StrictInt | None = Field(default=None, gt=0)
    disposition: Literal["pending", "coalesced", "ignored", "capacity_exhausted", "stale_binding", "stale_progress"]


_GitHubFanoutAdmission.model_rebuild()


def _github_target_digest(target: BaseModel) -> str:
    """Hash the complete canonical target identity used to distinguish admissions in one delivery."""
    encoded = json.dumps(target.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validated_github_fanout_page(value: object) -> _GitHubFanoutPage:
    """Strictly validate persisted fanout JSON before using its cursors, bindings, or admissions."""
    try:
        return _GitHubFanoutPage.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("github_fanout_page_invalid") from exc


def _append_github_fanout_admission(
    page: _GitHubFanoutPage,
    *,
    source_id: UUID,
    target_digest: str,
    result: "GitHubHintEnqueueResult",
) -> _GitHubFanoutPage:
    """Create a new validated page value with one atomic terminal admission result."""
    if any(entry.source_id == source_id and entry.target_sha256 == target_digest for entry in page.admissions):
        raise ValueError("github_fanout_admission_exists")
    return _validated_github_fanout_page({
        **page.model_dump(mode="python"),
        "admissions": (*page.admissions, _GitHubFanoutAdmission(
            source_id=source_id, target_sha256=target_digest, result=result,
        )),
    })


class GitHubHintClaim(BaseModel):
    """Carry an exact dirty revision claim into the current provider collection receipt UoW."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_generation: int
    connector_revision: int
    repository_id: str
    installation_id: str
    binding_revision: int
    hint_id: UUID
    dirty_revision: int
    resource: str
    locator_kind: str
    locator: str
    intent: str
    reconcile_page: int = Field(ge=1, le=101)
    lease_token: UUID
    expires_at: datetime


class PackagedCollectionWakeResult(BaseModel):
    """Describe an n8n wake attempt without implying provider fetch or indexing completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["acknowledged", "deferred", "ambiguous", "unavailable"]
    next_eligible_at: datetime | None = None
    receipt_id: str | None = Field(default=None, max_length=128)
    run_id: UUID | None = None
    batch_id: UUID | None = None
    status: str | None = Field(default=None, max_length=32)


async def persist_verified_github_delivery(session: AsyncSession, delivery: "VerifiedGitHubDelivery") -> "GitHubWebhookReceipt":
    """Atomically reserve durable global capacity and store an authenticated delivery digest.

    A global counter row serializes capacity decisions before the digest and source-less outbox
    are committed. Raw payload bytes and secrets are never persisted. Exact replay returns the
    original receipt; reusing an ID for different bytes conflicts.
    """
    from fastapi import HTTPException
    from modules.connectors.github.webhooks import GitHubWebhookReceipt, VerifiedGitHubDelivery

    try:
        normalized = VerifiedGitHubDelivery.model_validate(delivery.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="GitHub delivery metadata is invalid") from exc
    existing = await session.scalar(select(GithubWebhookDelivery).where(
        GithubWebhookDelivery.receiver_revision == normalized.receiver_revision,
        GithubWebhookDelivery.delivery_id == normalized.delivery_id,
    ))
    capacity = await session.scalar(select(GithubWebhookCapacity).where(GithubWebhookCapacity.id == 1).with_for_update())
    if capacity is None:
        await session.rollback()
        raise HTTPException(status_code=503, detail="GitHub webhook capacity is unavailable")
    # Recheck after acquiring the one durable reservation lock so concurrent duplicates serialize.
    existing = await session.scalar(select(GithubWebhookDelivery).where(
        GithubWebhookDelivery.receiver_revision == normalized.receiver_revision,
        GithubWebhookDelivery.delivery_id == normalized.delivery_id,
    ))
    if existing is not None:
        existing_raw_sha256 = existing.raw_sha256
        existing_id = existing.id
        existing_received_at = existing.received_at
        await session.rollback()
        if existing_raw_sha256 != normalized.raw_sha256:
            raise HTTPException(status_code=409, detail="GitHub delivery ID was reused with different bytes")
        return GitHubWebhookReceipt(
            delivery_id=normalized.delivery_id, receipt_id=existing_id, disposition="duplicate",
            accepted_at=existing_received_at,
        )
    pending = normalized.disposition == "received" and bool(normalized.targets)
    if capacity.digest_count >= 100_000 or pending and capacity.pending_count >= 100_000:
        await session.rollback()
        raise HTTPException(status_code=503, detail="GitHub webhook capacity is exhausted")
    receipt_id = uuid4()
    received_at = normalized.received_at
    session.add(GithubWebhookDelivery(
        id=receipt_id, receiver_revision=normalized.receiver_revision,
        delivery_id=normalized.delivery_id, raw_sha256=normalized.raw_sha256,
        event=normalized.event, action=normalized.action, app_id=normalized.app_id,
        installation_id=normalized.installation_id, repository_id=normalized.repository_id,
        targets=[target.model_dump(mode="json") for target in normalized.targets],
        disposition=normalized.disposition, received_at=received_at,
        detail_expires_at=received_at + timedelta(days=30),
    ))
    if pending:
        session.add(GithubWebhookOutbox(
            id=uuid4(), delivery_id=receipt_id, binding_cursor=None,
            state="pending", attempts=0, next_attempt_at=received_at,
        ))
        capacity.pending_count += 1
    capacity.digest_count += 1
    await session.commit()
    return GitHubWebhookReceipt(
        delivery_id=normalized.delivery_id, receipt_id=receipt_id,
        disposition=normalized.disposition, accepted_at=received_at,
    )


async def list_github_event_bindings(
    session: AsyncSession,
    *,
    app_id: str,
    installation_id: str,
    repository_id: str | None,
    limit: int = 50,
    cursor: str | None = None,
) -> GitHubEventBindingPage:
    """Page all matching historical or current installation bindings without returning token data.

    Opaque cursors bind their keyset position to the exact App, installation and optional
    repository query. Paused, expired and revoked grants stay visible for access-loss fencing.
    """
    import base64
    from modules.connectors.models import GithubOAuthGrant
    from modules.connectors.provisioning import activation_status
    from modules.sources import public as sources

    if isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("GitHub binding page limit must be between 1 and 50")
    after: UUID | None = None
    if cursor is not None:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if decoded.get("app") != app_id or decoded.get("installation") != installation_id or decoded.get("repository") != repository_id:
                raise ValueError
            after = UUID(decoded["after"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub binding cursor is invalid") from exc
    query = select(GithubOAuthGrant).where(
        GithubOAuthGrant.app_id == app_id,
        GithubOAuthGrant.installation_id == installation_id,
    )
    if repository_id is not None:
        query = query.where(GithubOAuthGrant.repository_id == repository_id)
    if after is not None:
        query = query.where(GithubOAuthGrant.source_id > after)
    grants = list((await session.scalars(query.order_by(GithubOAuthGrant.source_id).limit(limit + 1))).all())
    has_more = len(grants) > limit
    page = grants[:limit]
    bindings: list[GitHubEventBinding] = []
    for grant in page:
        source = await sources.get_connector_source(session, grant.source_id)
        if source is None:
            continue
        provisioned = await activation_status(session, grant.source_id)
        revision = provisioned.desired_revision if provisioned is not None else grant.configuration_revision
        try:
            from modules.connectors.github.schemas import project_github_source_config
            config = project_github_source_config(source.configuration)
            resources = tuple(name for name, enabled in (
                ("issue", config.include_issues), ("pull", config.include_pulls),
                ("commit", config.include_commits), ("release", config.include_releases),
            ) if enabled)
            bindings.append(GitHubEventBinding(
                source_id=source.id, source_status=source.status,
                source_generation=source.generation, connector_revision=revision,
                grant_state=grant.state, grant_expires_at=grant.expires_at,
                repository_id=grant.repository_id, installation_id=grant.installation_id,
                app_id=grant.app_id, binding_revision=grant.binding_revision,
                resource_scope=resources,
            ))
        except (TypeError, ValueError):
            continue
    next_cursor = None
    if has_more and page:
        token = json.dumps({
            "app": app_id, "installation": installation_id,
            "repository": repository_id, "after": str(page[-1].source_id),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        next_cursor = base64.urlsafe_b64encode(token).decode().rstrip("=")
        if len(next_cursor) > 512:
            raise ValueError("GitHub binding cursor exceeds its bound")
    return GitHubEventBindingPage(items=tuple(bindings), next_cursor=next_cursor)


async def _reserve_github_hint_slot(
    session: AsyncSession,
    *,
    outbox: GithubWebhookOutbox,
) -> bool:
    """Reserve one hint slot or transfer the already locked unfinished outbox reservation.

    Call after Source, provisioning/grant, outbox, and target hint locks. This helper then locks
    only global capacity; false leaves both reservation rows unchanged.
    """
    transfer = bool(
        outbox.capacity_reserved
        and outbox.state in {"pending", "dispatched", "needs_attention"}
    )
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).with_for_update())
    if capacity is None or capacity.pending_count >= 100_000 and not transfer:
        return False
    if transfer:
        outbox.capacity_reserved = False
    else:
        capacity.pending_count += 1
    return True


def _store_github_fanout_admission(
    outbox: GithubWebhookOutbox,
    page: _GitHubFanoutPage,
    *,
    source_id: UUID,
    target_digest: str,
    result: GitHubHintEnqueueResult,
) -> None:
    """Assign a new validated page value containing one atomic terminal admission result."""
    updated = _append_github_fanout_admission(
        page, source_id=source_id, target_digest=target_digest, result=result,
    )
    outbox.fanout_page = updated.model_dump(mode="json")


async def enqueue_github_hint(
    session: AsyncSession,
    *,
    binding: GitHubEventBinding,
    delivery_receipt_id: UUID,
    target: "GitHubTargetHint",
    expected_binding_cursor: str | None,
) -> GitHubHintEnqueueResult:
    """Atomically admit one distinct target within a frozen delivery binding page.

    Persisted page entries deduplicate delivery/source/target combinations across interleaved
    delivery retries. First admissions revalidate current authority and commit hint, capacity
    ownership, and result together. Lock order is Source, provisioning/grant, outbox, target hint,
    then capacity; a frozen detached binding page is bookkeeping, never provider authority.
    """
    from fastapi import HTTPException
    from modules.connectors.github.webhooks import GitHubTargetHint
    from modules.connectors.models import GithubOAuthGrant
    from modules.sources import public as sources

    try:
        binding = GitHubEventBinding.model_validate(binding.model_dump(mode="python"))
        target = GitHubTargetHint.model_validate(target.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="GitHub fanout identity is invalid") from exc

    source_locked = await sources.lock_source(session, binding.source_id)
    source = await sources.get_connector_source(session, binding.source_id) if source_locked is not None else None
    provisioned = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == binding.source_id,
    ).with_for_update())
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == binding.source_id,
    ).with_for_update())
    outbox = await session.scalar(select(GithubWebhookOutbox).where(
        GithubWebhookOutbox.delivery_id == delivery_receipt_id,
    ).with_for_update())
    if outbox is None or outbox.state not in {"pending", "dispatched", "needs_attention"}:
        await session.rollback()
        return GitHubHintEnqueueResult(disposition="stale_progress")
    try:
        page = _validated_github_fanout_page(outbox.fanout_page)
    except ValueError:
        await session.rollback()
        return GitHubHintEnqueueResult(disposition="stale_progress")
    if (
        outbox.binding_cursor != expected_binding_cursor
        or page.cursor_before != expected_binding_cursor
        or not any(
            item.model_dump(mode="json") == binding.model_dump(mode="json")
            for item in page.bindings
        )
    ):
        await session.rollback()
        return GitHubHintEnqueueResult(disposition="stale_progress")

    delivery = await session.get(GithubWebhookDelivery, delivery_receipt_id)
    if delivery is None or not any(item == target.model_dump(mode="json") for item in delivery.targets):
        await session.rollback()
        raise HTTPException(status_code=409, detail="GitHub delivery target is no longer current")
    target_digest = _github_target_digest(target)
    prior = next((
        entry for entry in page.admissions
        if entry.source_id == binding.source_id and entry.target_sha256 == target_digest
    ), None)
    if prior is not None:
        result = prior.result.model_copy(deep=True)
        await session.rollback()
        return result

    def persist_terminal_admission(result: GitHubHintEnqueueResult) -> GitHubHintEnqueueResult:
        """Attach an authoritative nonretryable result to this page before its transaction commits."""
        _store_github_fanout_admission(
            outbox, page, source_id=binding.source_id,
            target_digest=target_digest, result=result,
        )
        return result

    access_loss = target.intent == "visibility_lost"
    binding_is_current = not (
        source is None or source.provider != "github"
        or source.status == "archived"
        or source.generation != binding.source_generation
        or provisioned is None or provisioned.desired_revision != binding.connector_revision
        or provisioned.source_generation != binding.source_generation
        or grant is None or grant.repository_id != binding.repository_id
        or grant.installation_id != binding.installation_id or grant.app_id != binding.app_id
        or grant.binding_revision != binding.binding_revision
        or grant.configuration_revision != binding.connector_revision
        or (not access_loss and (
            source.status != "active" or grant.state != "ready"
            or grant.expires_at is None or grant.expires_at <= datetime.now(UTC)
        ))
        or (source.status == "paused" and not access_loss)
    )
    if not binding_is_current:
        result = persist_terminal_admission(GitHubHintEnqueueResult(disposition="stale_binding"))
        await session.commit()
        return result

    from modules.connectors.github.schemas import project_github_source_config
    try:
        config = project_github_source_config(source.configuration)
    except (TypeError, ValueError):
        result = persist_terminal_admission(GitHubHintEnqueueResult(disposition="stale_binding"))
        await session.commit()
        return result
    enabled_resources = {
        name for name, enabled in (
            ("issue", config.include_issues), ("pull", config.include_pulls),
            ("commit", config.include_commits), ("release", config.include_releases),
        ) if enabled
    }
    if target.intent not in {"visibility_lost", "visibility_check"} and target.resource not in enabled_resources:
        result = persist_terminal_admission(GitHubHintEnqueueResult(disposition="ignored"))
        await session.commit()
        return result

    if access_loss:
        from core.realtime import commit_with_replay, make_source_change

        visibility_hint = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.source_id == binding.source_id,
            GithubSourceHint.resource == target.resource,
            GithubSourceHint.locator_kind == target.locator_kind,
            GithubSourceHint.locator == target.locator,
        ).with_for_update())
        paused = await sources.pause_source_for_connector(session, binding.source_id)
        if paused is None:
            result = persist_terminal_admission(GitHubHintEnqueueResult(disposition="stale_binding"))
            await session.commit()
            return result
        if visibility_hint is None:
            session.add(GithubSourceHint(
                id=uuid4(), source_id=binding.source_id,
                source_generation=paused.generation,
                connector_revision=binding.connector_revision,
                repository_id=binding.repository_id, binding_revision=binding.binding_revision,
                resource=target.resource, locator_kind=target.locator_kind, locator=target.locator,
                intent="visibility_lost", dirty_revision=1,
                last_delivery_id=delivery_receipt_id, state="visibility_unverified",
                capacity_reserved=False, next_attempt_at=datetime.now(UTC),
            ))
        else:
            visibility_hint.dirty_revision += 1
            visibility_hint.source_generation = paused.generation
            visibility_hint.connector_revision = binding.connector_revision
            visibility_hint.repository_id = binding.repository_id
            visibility_hint.binding_revision = binding.binding_revision
            visibility_hint.intent = "visibility_lost"
            visibility_hint.last_delivery_id = delivery_receipt_id
            visibility_hint.state = "visibility_unverified"
            visibility_hint.claim_token = None
            visibility_hint.claimed_revision = None
            visibility_hint.claim_expires_at = None
            if visibility_hint.capacity_reserved:
                capacity = await session.scalar(select(GithubWebhookCapacity).where(
                    GithubWebhookCapacity.id == 1,
                ).with_for_update())
                if capacity is not None and capacity.pending_count > 0:
                    capacity.pending_count -= 1
                visibility_hint.capacity_reserved = False
        result = persist_terminal_admission(GitHubHintEnqueueResult(disposition="ignored"))
        if source.status == "active":
            await commit_with_replay(session, [make_source_change(paused.id, paused.generation, paused.status)])
        else:
            await session.commit()
        return result

    existing = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.source_id == binding.source_id,
        GithubSourceHint.resource == target.resource,
        GithubSourceHint.locator_kind == target.locator_kind,
        GithubSourceHint.locator == target.locator,
    ).with_for_update())
    if existing is not None:
        if not existing.capacity_reserved and not await _reserve_github_hint_slot(session, outbox=outbox):
            await session.rollback()
            return GitHubHintEnqueueResult(disposition="capacity_exhausted")
        existing.capacity_reserved = True
        existing.dirty_revision += 1
        existing.last_delivery_id = delivery_receipt_id
        existing.intent = target.intent
        existing.source_generation = binding.source_generation
        existing.connector_revision = binding.connector_revision
        existing.repository_id = binding.repository_id
        existing.binding_revision = binding.binding_revision
        existing.state = "pending"
        existing.next_attempt_at = datetime.now(UTC)
        result = persist_terminal_admission(GitHubHintEnqueueResult(
            hint_id=existing.id, dirty_revision=existing.dirty_revision,
            disposition="coalesced",
        ))
        await session.commit()
        return result

    count = await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
        GithubSourceHint.source_id == binding.source_id,
        GithubSourceHint.state.in_(("pending", "dispatched", "accepted_ingestion", "needs_attention", "capacity_deferred")),
    )) or 0
    locator_kind, locator, intent = target.locator_kind, target.locator, target.intent
    if count >= 100:
        locator_kind, locator, intent = "repository", binding.repository_id, "reconcile"
        existing = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.source_id == binding.source_id,
            GithubSourceHint.resource == target.resource,
            GithubSourceHint.locator_kind == locator_kind,
            GithubSourceHint.locator == locator,
        ).with_for_update())
        if existing is not None:
            if not existing.capacity_reserved and not await _reserve_github_hint_slot(session, outbox=outbox):
                await session.rollback()
                return GitHubHintEnqueueResult(disposition="capacity_exhausted")
            existing.capacity_reserved = True
            existing.dirty_revision += 1
            existing.last_delivery_id = delivery_receipt_id
            existing.source_generation = binding.source_generation
            existing.connector_revision = binding.connector_revision
            existing.repository_id = binding.repository_id
            existing.binding_revision = binding.binding_revision
            existing.intent = "reconcile"
            existing.state = "pending"
            existing.reconcile_page = 1
            existing.next_attempt_at = datetime.now(UTC)
            result = persist_terminal_admission(GitHubHintEnqueueResult(
                hint_id=existing.id, dirty_revision=existing.dirty_revision,
                disposition="coalesced",
            ))
            await session.commit()
            return result

    if not await _reserve_github_hint_slot(session, outbox=outbox):
        await session.rollback()
        return GitHubHintEnqueueResult(disposition="capacity_exhausted")
    hint = GithubSourceHint(
        id=uuid4(), source_id=binding.source_id,
        source_generation=binding.source_generation,
        connector_revision=binding.connector_revision,
        repository_id=binding.repository_id,
        binding_revision=binding.binding_revision,
        resource=target.resource, locator_kind=locator_kind, locator=locator,
        intent=intent, dirty_revision=1, last_delivery_id=delivery_receipt_id,
        state="pending", capacity_reserved=True, next_attempt_at=datetime.now(UTC),
    )
    session.add(hint)
    result = persist_terminal_admission(GitHubHintEnqueueResult(
        hint_id=hint.id, dirty_revision=hint.dirty_revision, disposition="pending",
    ))
    await session.commit()
    return result

async def claim_github_hint(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    now: datetime | None = None,
) -> GitHubHintClaim | None:
    """Commit one exact due hint claim after collection reservation and current grant fencing.

    Claiming never authorizes the provider read. The caller must reserve the normal ingestion
    collection lease first; receipt acceptance independently verifies that exact lease.
    Lock order is Source, provisioning, grant, then hint.
    """
    from modules.connectors.models import GithubOAuthGrant
    from modules.sources import public as sources

    now = (now or datetime.now(UTC)).astimezone(UTC)
    if await sources.lock_source(session, source_id) is None:
        await session.rollback()
        return None
    fence = await get_github_binding_fence(
        session, source_id, source_generation=source_generation,
        connector_revision=connector_revision, lock=True,
    )
    if fence is None:
        await session.rollback()
        return None
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).with_for_update())
    if grant is None or grant.binding_revision != fence.binding_revision:
        await session.rollback()
        return None
    hint = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source_id,
        GithubSourceHint.source_generation == source_generation,
        GithubSourceHint.connector_revision == connector_revision,
        GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention", "capacity_deferred")),
        or_(GithubSourceHint.capacity_reserved.is_(True), GithubSourceHint.state == "capacity_deferred"),
        GithubSourceHint.reconcile_page <= 100,
        GithubSourceHint.next_attempt_at <= now,
        (GithubSourceHint.claim_token.is_(None) | (GithubSourceHint.claim_expires_at <= now)),
    ).order_by(GithubSourceHint.updated_at, GithubSourceHint.id).limit(1).with_for_update())
    if hint is None:
        await session.rollback()
        return None
    if hint.state == "capacity_deferred" and not hint.capacity_reserved:
        if (
            hint.repository_id != fence.repository_id
            or hint.binding_revision != fence.binding_revision
            or hint.resource not in fence.resource_scope
        ):
            hint.state = "visibility_unverified"
            hint.next_attempt_at = now
            await session.commit()
            return None
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update())
        if capacity is None or capacity.pending_count >= 100_000:
            hint.next_attempt_at = now + timedelta(seconds=60)
            await session.commit()
            return None
        capacity.pending_count += 1
        hint.capacity_reserved = True
        hint.state = "pending"
    token = uuid4()
    expires = now + timedelta(seconds=60)
    hint.claim_token = token
    hint.claimed_revision = hint.dirty_revision
    hint.claim_expires_at = expires
    hint.state = "dispatched"
    await session.commit()
    return GitHubHintClaim(
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, repository_id=fence.repository_id,
        installation_id=fence.installation_id, binding_revision=fence.binding_revision,
        hint_id=hint.id, dirty_revision=hint.dirty_revision, resource=hint.resource,
        locator_kind=hint.locator_kind, locator=hint.locator, intent=hint.intent,
        reconcile_page=hint.reconcile_page,
        lease_token=token, expires_at=expires,
    )


async def reconcile_github_source_hints_lifecycle(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    active: bool,
) -> int:
    """Retire source hints on pause/archive and rearm them only under a current verified binding.

    The caller holds the Source lock and has updated/fenced its provisioning row. This method
    continues Source -> provisioning -> GitHub grant -> hint -> capacity order. Pausing clears
    exact claims and releases only slots owned by those hints; reactivation reserves fresh global
    capacity only after the active source/grant fence matches the source's new generation. If the
    global capacity is full, the durable capacity_deferred state retains current fences for the
    bounded worker and owner-claim reservation retries.
    """
    from modules.connectors.models import GithubOAuthGrant
    from modules.connectors.provisioning import activation_status
    if active:
        provisioned = await activation_status(session, source_id)
        if provisioned is None:
            return 0
        fence = await get_github_binding_fence(
            session, source_id, source_generation=source_generation,
            connector_revision=provisioned.desired_revision, lock=True,
        )
        if fence is None:
            return 0
        candidates = list((await session.scalars(select(GithubSourceHint).where(
            GithubSourceHint.source_id == source_id,
            GithubSourceHint.state.in_(("paused", "capacity_deferred")),
        ).order_by(GithubSourceHint.updated_at, GithubSourceHint.id).limit(100).with_for_update())).all())
        if not candidates:
            return 0
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update())
        if capacity is None:
            return 0
        rearmed = 0
        now = datetime.now(UTC)
        for hint in candidates:
            hint.source_generation = source_generation
            hint.connector_revision = fence.connector_revision
            hint.claim_token = None
            hint.claimed_revision = None
            hint.claim_expires_at = None
            if (
                hint.repository_id != fence.repository_id
                or hint.binding_revision != fence.binding_revision
                or hint.resource not in fence.resource_scope
            ):
                hint.state = "visibility_unverified"
                if hint.capacity_reserved:
                    capacity.pending_count = max(0, capacity.pending_count - 1)
                    hint.capacity_reserved = False
                continue
            hint.attempts = 0
            hint.reconcile_page = 1
            if not hint.capacity_reserved and capacity.pending_count >= 100_000:
                hint.state = "capacity_deferred"
                hint.next_attempt_at = now + timedelta(seconds=60)
                continue
            if not hint.capacity_reserved:
                capacity.pending_count += 1
                hint.capacity_reserved = True
            hint.state = "pending"
            hint.next_attempt_at = now
            rearmed += 1
        return rearmed

    provisioned = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update())
    if provisioned is None:
        return 0
    await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).with_for_update())
    active_hints = list((await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source_id,
        GithubSourceHint.state.in_(("pending", "dispatched", "accepted_ingestion", "needs_attention", "capacity_deferred")),
    ).order_by(GithubSourceHint.id).with_for_update())).all())
    releases = sum(hint.capacity_reserved for hint in active_hints)
    for hint in active_hints:
        hint.state = "paused"
        hint.claim_token = None
        hint.claimed_revision = None
        hint.claim_expires_at = None
        hint.capacity_reserved = False
    if releases:
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update())
        if capacity is not None:
            capacity.pending_count = max(0, capacity.pending_count - releases)
    return len(active_hints)


async def acknowledge_github_hint(
    session: AsyncSession,
    *,
    claim: GitHubHintClaim,
    batch_id: UUID | None,
    disposition: Literal["accepted_ingestion", "completed", "ignored", "visibility_unverified"],
    reconcile_next_page: int | None = None,
) -> bool:
    """Flush an exact claim acknowledgement inside the receipt or visibility owner transaction.

    Lost or expired claims return false. If a newer delivery dirtied the target, this claim only
    releases its token and leaves the newer revision pending. Bounded reconcile pages advance the
    durable resource page while retaining its capacity slot; page 101 records incomplete coverage.
    """
    hint = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.id == claim.hint_id,
    ).with_for_update())
    now = datetime.now(UTC)
    if (
        hint is None or hint.claim_token is None or hint.claim_token != claim.lease_token
        or hint.claimed_revision != claim.dirty_revision
        or hint.claim_expires_at is None or hint.claim_expires_at <= now
        or hint.source_generation != claim.source_generation
        or hint.connector_revision != claim.connector_revision
        or hint.repository_id != claim.repository_id
        or hint.binding_revision != claim.binding_revision
    ):
        return False
    if hint.dirty_revision != claim.dirty_revision:
        # A newer delivery owns the page pointer and reservation; an old receipt releases only its claim.
        hint.claim_token = None
        hint.claimed_revision = None
        hint.claim_expires_at = None
        hint.state = "pending"
        hint.next_attempt_at = now
        return True
    if hint.reconcile_page != claim.reconcile_page:
        return False
    hint.claim_token = None
    hint.claimed_revision = None
    hint.claim_expires_at = None
    hint.acknowledged_batch_id = batch_id
    if claim.intent == "reconcile" and reconcile_next_page is not None:
        if reconcile_next_page != claim.reconcile_page + 1 or reconcile_next_page > 101:
            return False
        hint.reconcile_page = reconcile_next_page
        hint.state = "pending" if reconcile_next_page <= 100 else "needs_attention"
        hint.next_attempt_at = now + timedelta(seconds=30 if reconcile_next_page <= 100 else 3600)
        if reconcile_next_page > 100 and hint.capacity_reserved:
            capacity = await session.scalar(select(GithubWebhookCapacity).where(
                GithubWebhookCapacity.id == 1,
            ).with_for_update())
            if capacity is not None and capacity.pending_count > 0:
                capacity.pending_count -= 1
            hint.capacity_reserved = False
        return True
    hint.state = disposition
    hint.next_attempt_at = now
    if disposition in {"accepted_ingestion", "completed", "ignored", "visibility_unverified"} and hint.capacity_reserved:
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update())
        if capacity is not None and capacity.pending_count > 0:
            capacity.pending_count -= 1
        hint.capacity_reserved = False
    return True


async def wake_packaged_collection(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    settings: "Settings",
    timeout_seconds: int = 15,
) -> PackagedCollectionWakeResult:
    """Wake only an active, fully applied packaged n8n collection through its real webhook token.

    Source and provisioning locks are released before network I/O. The wake carries only source
    fences; n8n continues through its actual provider-fetch route and collector credential. A
    timeout is ambiguous and must keep the durable hint pending for idempotent retry.
    """
    import httpx
    from modules.connectors.n8n import workflow_webhook_path
    from modules.connectors.provisioning import activation_status
    from modules.sources import public as sources

    if not 1 <= timeout_seconds <= 75:
        raise ValueError("Packaged collection wake timeout is out of range")
    locked = await sources.lock_source(session, source_id)
    source = await sources.get_connector_source(session, source_id) if locked is not None else None
    provisioned = await activation_status(session, source_id) if source is not None else None
    if (
        source is None or source.status != "active" or source.generation != source_generation
        or provisioned is None or provisioned.state != "active"
        or provisioned.applied_revision != connector_revision
        or provisioned.desired_revision != connector_revision
        or provisioned.source_generation != source_generation
        or not await require_collection_fence(
            session, source, CollectionFence(
                source_generation=source_generation, connector_revision=connector_revision,
            ), lock=True,
        )
    ):
        await session.rollback()
        return PackagedCollectionWakeResult(outcome="unavailable")
    source_type = source.type
    await session.rollback()
    token = settings.n8n_webhook_token.get_secret_value()
    if not token:
        return PackagedCollectionWakeResult(outcome="unavailable")
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{str(settings.n8n_service_url).rstrip('/')}/webhook/{workflow_webhook_path(source_id, source_type)}",
                json={
                    "source_id": str(source_id), "source_generation": source_generation,
                    "connector_revision": connector_revision,
                },
                headers={"X-BBD-Webhook-Token": token},
            )
            if response.status_code in {409, 425, 429}:
                retry_at = None
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdecimal():
                    retry_at = datetime.now(UTC) + timedelta(seconds=min(int(retry_after), 3600))
                return PackagedCollectionWakeResult(outcome="deferred", next_eligible_at=retry_at)
            response.raise_for_status()
            metadata: dict[str, object] = {}
            try:
                body = response.json()
                if isinstance(body, dict):
                    metadata = body
            except ValueError:
                pass
            try:
                run_id = UUID(str(metadata["run_id"])) if metadata.get("run_id") else None
                batch_id = UUID(str(metadata["batch_id"])) if metadata.get("batch_id") else None
            except (TypeError, ValueError):
                run_id = batch_id = None
            status = metadata.get("status")
            receipt_id = metadata.get("receipt_id")
            return PackagedCollectionWakeResult(
                outcome="acknowledged", run_id=run_id, batch_id=batch_id,
                status=status[:32] if isinstance(status, str) else "queued",
                receipt_id=receipt_id[:128] if isinstance(receipt_id, str) else None,
            )
    except httpx.TimeoutException:
        return PackagedCollectionWakeResult(outcome="ambiguous")
    except httpx.HTTPError:
        return PackagedCollectionWakeResult(outcome="unavailable")


async def require_batch_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    connector_revision: int | None,
) -> bool:
    """Require a revision for managed connector sources while preserving native ingestion."""
    from modules.connectors import provisioning

    row = await provisioning.activation_status(session, source.id)
    if row is None:
        return connector_revision is None and source.generation == source_generation
    if connector_revision is None:
        return False
    return await provisioning.require_collection_fence(
        session, source, source_generation, connector_revision, lock=True
    )


async def fence_source_collection(session: AsyncSession, source: SourceFence) -> bool:
    """Persist connector-owned deactivation after the source owner has fenced a source."""
    from modules.connectors import provisioning

    return await provisioning.fence_source_collection(session, source)


async def save_connector_configuration(
    session: AsyncSession,
    source: ConnectorSource,
    expected_revision: int,
    source_configuration: dict[str, object],
    desired_configuration: dict[str, object],
    *,
    allow_paused: bool = False,
) -> tuple[ConnectorSource, ConnectorProvisioning] | None:
    """Save source and desired connector settings, rolling back revision conflicts."""
    from modules.connectors import provisioning
    from modules.sources import public as source_public

    saved = await source_public.set_connector_configuration(
        session, source.id, source.generation, source_configuration,
        allow_paused=allow_paused,
    )
    if saved is None:
        return None
    row = await provisioning.save_desired(
        session,
        source.id,
        saved.generation,
        expected_revision,
        desired_configuration,
    )
    if row is None:
        await session.rollback()
        return None
    return saved, row


async def allow_external_collector_credential_issue(
    session: AsyncSession, source_id: UUID
) -> bool:
    """Allow external token issuance only for active sources not under provisioning."""
    from modules.connectors import provisioning

    source, row, _ = await provisioning.lock_connector(session, source_id)
    return bool(
        source is not None and source.status == "active"
        and (row is None or (not row.desired_enabled and row.state != "provisioning"))
    )


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


async def validate_public_url(value: str) -> str:
    """Require credential-free HTTP(S) URLs whose resolved addresses are all public."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTP(S) URLs are allowed")
    try:
        addresses = await asyncio.to_thread(
            getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), 0, 0, 0
        )
    except OSError as exc:
        raise ValueError("URL host could not be resolved") from exc
    if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("URL resolves to a non-public address")
    return value


def overlap_floor(cursor: str | None) -> datetime | None:
    """Return a one-day UTC overlap start for a valid aware cursor, otherwise None."""
    if not cursor:
        return None
    try:
        value = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC) - DEFAULT_OVERLAP


async def map_github_version(session: AsyncSession, ready: object) -> bool:
    """Map a current ready GitHub document version into canonical knowledge (flush-only, idempotent).

    Public entry for the ready-version worker; non-github sources return False.
    """
    from modules.connectors.github.mapping import map_github_version as _map

    return await _map(session, ready)  # type: ignore[arg-type]
