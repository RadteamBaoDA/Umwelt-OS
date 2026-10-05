from datetime import UTC, datetime, timedelta
import hashlib
import json
from ipaddress import ip_address
from socket import getaddrinfo
from urllib.parse import urlsplit
from uuid import UUID
import asyncio
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StrictInt, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from modules.sources.schemas import ConnectorSource, SourceFence
from modules.connectors.models import AgentBrowserGrant, ConnectorProvisioning

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_OVERLAP = timedelta(days=1)


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


@dataclass(frozen=True)
class ConnectorConfigurationSnapshot:
    """Expose persisted connector settings and activation state without secret values."""
    source_id: UUID
    source_type: str
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
    configuration = ConnectorConfig.model_validate(source.configuration).model_dump(
        mode="json", exclude_none=True
    )
    if "schedule_interval_minutes" not in configuration:
        configuration["schedule_interval_minutes"] = default_schedule_interval_minutes(
            source.type
        )
    desired = row.desired_configuration if row is not None else {}
    return ConnectorConfigurationSnapshot(
        source_id=source.id,
        source_type=source.type,
        source_generation=source.generation,
        configuration=configuration,
        expected_revision=row.desired_revision if row is not None else 0,
        auth_method=str(desired.get("auth_method", "none")),
        auth_header_name=(
            str(desired["auth_header_name"])
            if desired.get("auth_header_name") is not None
            else None
        ),
        desired_enabled=bool(row and row.desired_enabled),
        activation_state=row.state if row is not None else "saved_not_active",
        activation_error_code=row.error_code if row is not None else None,
        provider_credential_configured=bool(
            provider_credential is not None
            and provider_credential.state == "ready"
            and provider_credential.credential_id
        ),
        provider_credential_state=(
            provider_credential.state if provider_credential is not None else None
        ),
    )


def default_schedule_interval_minutes(source_type: str) -> int:
    """Return the default polling cadence for RSS versus other source types."""
    return 15 if source_type == "rss" else 30


class ConnectorRecord(BaseModel):
    """Validate one normalized record returned by a connector collector."""
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=1_000_000)
    observed_at: datetime
    version: str | None = Field(default=None, max_length=255)
    metadata: dict[str, object] = Field(default_factory=dict)


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
