"""Provider scope projection/export and agent-browser grant operations.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    NotFoundError,
)
from core.workspaces.schemas import Scope
from modules.connectors.models import (
    AgentBrowserGrant,
)
from modules.connectors.providers.cii import CiiProjection
from modules.connectors.service.access import (
    _connector_actor,
    _read_scoped_source,
)
from modules.connectors.service.config import (
    get_connector_configuration,
)
from modules.connectors.service.schemas import (
    ProviderScopeSnapshot,
)


def cii_v8_availability(requested_countries: list[str]) -> CiiProjection:
    """Expose the CII provider's truthful unavailable v8 projection through the connector owner."""
    from modules.connectors.providers.cii import unavailable_v8_projection

    return unavailable_v8_projection(requested_countries)


# Providers whose scope is exactly one of several alternative fields (enforced by registry.validate).
_ALTERNATIVE_SCOPE_FIELDS: dict[str, frozenset[str]] = {
    "mastodon": frozenset({"mastodon_account", "mastodon_hashtag"}),
    "x": frozenset({"x_account", "x_query"}),
}


# Single source of truth for catalog scope fields that may be absent from a stored configuration. Lives here (not in
# registry.py, which imports this module) to avoid an import cycle; registry.validate imports it too.
OPTIONAL_SCOPE_FIELDS: dict[str, frozenset[str]] = {
    "github": frozenset({"github_history_days"}),
    "mastodon": frozenset({"mastodon_account", "mastodon_hashtag"}),
    "x": frozenset({"x_account", "x_query"}),
    "coingecko": frozenset({"coingecko_assets"}),  # absent on legacy BTC/USD sources (collection falls back to BTC)
}


def _scope_values(provider_id: str, scope_fields: Sequence[str], configuration: dict[str, Any]) -> dict[str, Any] | None:
    """Collect the configured scope fields, or None when a required one is missing.

    Fields in OPTIONAL_SCOPE_FIELDS (the same map registry.validate uses) may be absent and are simply left out of
    the values, so the scope hash stays deterministic; otherwise legacy CoinGecko sources without coin selections had
    no verifiable scope and every observations read failed closed.

    Alternative fields (Mastodon account or hashtag, X account or query) are optional individually but at least
    one must be present; requiring all of them made every such source's accepted scope unverifiable, which hid its
    ingested records from dashboard projections.
    """
    alternatives = _ALTERNATIVE_SCOPE_FIELDS.get(provider_id, frozenset())
    optional = OPTIONAL_SCOPE_FIELDS.get(provider_id, frozenset())
    values = {name: configuration[name] for name in scope_fields if name in configuration}
    if any(name not in values and name not in optional for name in scope_fields):
        return None
    if alternatives and not alternatives & values.keys():
        return None
    return values


async def get_current_provider_scope(
    session: AsyncSession, source_id: UUID, expected_source_generation: int,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ProviderScopeSnapshot | None:
    """Hash validated non-secret scope fields for supported active providers only.

    The result contains no connector configuration values. Unsupported providers,
    inactive sources, invalid config, and stale generations fail closed as None.
    """
    from modules.connectors.catalog import get_catalog_entry
    from modules.sources import public as sources

    fence = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if fence is None or fence.status != "active" or fence.generation != expected_source_generation:
        return None
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.status != fence.status or source.generation != fence.generation:
        return None
    provider_id = source.provider or {"rss": "rss", "web": "web", "api": "rest"}.get(source.type)
    entry = get_catalog_entry(provider_id) if provider_id else None
    if provider_id is None or entry is None or entry.availability not in {"available", "implemented", "requires_credentials"}:
        return None
    try:
        from modules.connectors.registry import configuration as provider_configuration

        configuration = provider_configuration(source).model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
        return None
    values = _scope_values(provider_id, entry.scope_fields, configuration)
    if values is None:
        return None
    encoded = json.dumps([provider_id, values], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ProviderScopeSnapshot(
        workspace_id=source.workspace_id, source_id=source.id, source_generation=source.generation,
        provider_id=provider_id, discriminator=hashlib.sha256(encoded.encode()).hexdigest(),
    )


async def export_provider_scope(
    session: AsyncSession, source_id: UUID, expected_source_generation: int,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ProviderScopeSnapshot | None:
    """Project retained non-secret provider scope for owner exports across inactive states.

    This does not authorize collection or reuse historical observation fields: it validates the
    current retained source generation/configuration and rejects pending data purge. A lifecycle
    pause/archive alone does not erase portable owner evidence; changed or unsupported scope fails closed.
    Credential bytes and provider configuration are never returned.
    """
    from modules.connectors.catalog import get_catalog_entry
    from modules.sources import public as sources
    from modules.sources.public import SourceExportFence

    if not await sources.filter_export_eligible_sources(session, [SourceExportFence(
        source_id=source_id, workspace_id=scope.workspace_id, generation=expected_source_generation,
    )], scope=scope, multi_workspace_enabled=multi_workspace_enabled):
        return None
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.generation != expected_source_generation:
        return None
    provider_id = source.provider or {"rss": "rss", "web": "web", "api": "rest"}.get(source.type)
    entry = get_catalog_entry(provider_id) if provider_id else None
    if provider_id is None or entry is None or entry.availability not in {"available", "implemented", "requires_credentials"}:
        return None
    try:
        from modules.connectors.registry import configuration as provider_configuration

        configuration = provider_configuration(source).model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
        return None
    values = _scope_values(provider_id, entry.scope_fields, configuration)
    if values is None:
        return None
    encoded = json.dumps([provider_id, values], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ProviderScopeSnapshot(
        workspace_id=source.workspace_id, source_id=source.id, source_generation=source.generation,
        provider_id=provider_id, discriminator=hashlib.sha256(encoded.encode()).hexdigest(),
    )


@dataclass(frozen=True)
class AgentBrowserScope:
    """Bind browser reads to current source and connector revisions and one exact HTTPS path."""

    workspace_id: UUID
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
    session: AsyncSession, owner_id: int, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentBrowserScope | None:
    """Resolve an enabled grant only while the active web source and connector revision match."""
    from modules.sources import public as sources

    if owner_id != _connector_actor(scope):
        return None
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.status != "active" or source.type != "web":
        return None
    configuration = await get_connector_configuration(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(AgentBrowserGrant).where(AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == owner_id).execution_options(populate_existing=True))
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
    # PRODUCTION FIX: ConnectorSource carries no local_only; read it from the source fence.
    fence = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if fence is None:
        return None
    return AgentBrowserScope(
        source.workspace_id, source_id, source.generation, configuration.expected_revision,
        row.grant_revision, scope_hash, origin, path_prefix,
        fence.local_only, row.enabled and not fence.local_only,
    )


async def invalidate_agent_browser_grant_in_uow(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Disable/revision-bump the exact actor's grant under caller-held admission/Source.

    Fresh Source/grant reads acquire no row locks. Source serialization protects this
    lifecycle mutation; the prepared visibility apply uses its own identical mutation body.
    """
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise NotFoundError("Source not found")
    row = await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == _connector_actor(scope),
    ).execution_options(populate_existing=True))
    if row is None:
        return
    row.enabled = False
    row.grant_revision += 1
    await session.flush()


async def update_agent_browser_grant_in_uow(
    session: AsyncSession, owner_id: int, source_id: UUID,
    expected_revision: int, grant: AgentBrowserGrantPatch,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentBrowserScopeRead:
    """Acquire admission/Source/provisioning then browser grant for explicit web opt-in.

    Enter before domain locks; the in_uow suffix means flush-only caller-owned commit,
    not an upstream-lock bypass. Actual scoped owner must equal the retained owner_id.
    """
    from modules.sources import public as sources

    if owner_id != _connector_actor(scope):
        raise PermissionError("Browser grant owner is unavailable")
    from modules.connectors import provisioning

    source, _provisioned, _slots = await provisioning.lock_connector(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source_view = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    configuration = await get_connector_configuration(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
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
    row = await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == owner_id,
    ).with_for_update().execution_options(populate_existing=True))
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
        source.workspace_id, source_id, source.generation, configuration.expected_revision,
        row.grant_revision, scope_hash, origin, path_prefix,
        source.local_only, row.enabled,
    )
