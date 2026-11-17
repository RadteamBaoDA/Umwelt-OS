"""Shared settings helpers: endpoint validation, credential cipher, scope/admission guards, provider resolution and execution-config derivation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import get_active_account, lock_account_admission
from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    UnauthorizedError,
    UnavailableError,
    ValidationFailedError,
)
from core.model_gateway.client import effective_provider_kind, hosted_endpoint_equivalent
from core.model_gateway.schemas import (
    DEFAULT_PROVIDER_ID,
    HOSTED_PROVIDER_ENDPOINTS,
    HOSTED_PROVIDER_KINDS,
    LOCAL_PROVIDER_KINDS,
    AIExecutionConfig,
    ModelMapping,
    PrivacySettings,
    ProviderConfig,
    ProviderKind,
    ProviderModel,
)
from core.workspaces.public import (
    lock_access_fence,
    read_access_fence,
)
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.settings.models import (
    AISettingsRecord,
)

VIZ_TYPESAFE_DESTINATION = "typesafe:api.typesafe.ai"


VIZ_OPENROUTER_URL = HOSTED_PROVIDER_ENDPOINTS["openrouter"]


VIZ_CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"


VIZ_CLOUDFLARE_ACCOUNT = re.compile(r"[0-9a-f]{32}")


VIZ_KEYED_KINDS = (
    "openrouter",
    "cloudflare",
)  # self-contained OpenAI-compatible routers with their own endpoint and key


VIZ_ROUTER_ROLE = "viz-router"  # telemetry/capability label of the router; not one of MODEL_ROLES


def _fingerprint(value: str) -> str:
    """Return a stable SHA-256 fingerprint without exposing the input value."""
    return hashlib.sha256(value.encode()).hexdigest()


def _endpoint(value: str | None, settings: Settings) -> str | None:
    """Validate and canonicalize an endpoint against deployment host policy.

    Rejects credentials, query/fragment components, invalid ports, and scoped
    IPv6; raises HTTP 422 for invalid or disallowed endpoints.
    """
    if not value:
        return None
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValidationFailedError(detail="Invalid gateway endpoint")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationFailedError(detail="Invalid gateway endpoint") from exc
    if port == 0:
        raise ValidationFailedError(detail="Gateway endpoint port must be between 1 and 65535")
    is_ip = True
    try:
        parsed_address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        normalized_host = parts.hostname.encode("idna").decode("ascii").lower()
        is_ipv6 = is_ip = False
    else:
        if (
            isinstance(parsed_address, ipaddress.IPv6Address)
            and parsed_address.scope_id is not None
        ):
            raise ValidationFailedError(detail="Scoped IPv6 gateway endpoints are not allowed")
        normalized_host = parsed_address.compressed.lower()
        is_ipv6 = isinstance(parsed_address, ipaddress.IPv6Address)
    default_port = 443 if parts.scheme.lower() == "https" else 80
    authority_host = f"[{normalized_host}]" if is_ipv6 else normalized_host
    effective_port = port if port is not None else default_port
    policy_authority = (
        authority_host if effective_port == default_port else f"{authority_host}:{effective_port}"
    )
    effective_authority = f"{authority_host}:{effective_port}"
    # Cloud providers: https public hostnames pass when the operator opted in (transport still pins the checked IPs).
    public_ok = settings.ai_allow_public_providers and parts.scheme.lower() == "https" and not is_ip
    if (
        policy_authority not in settings.ai_allowed_endpoint_hosts
        and effective_authority not in settings.ai_allowed_endpoint_hosts
        and not public_ok
    ):
        raise ValidationFailedError(detail="Endpoint address is not allowed by deployment policy")
    return urlunsplit((parts.scheme.lower(), policy_authority, parts.path.rstrip("/"), "", ""))


def validate_endpoint(value: str | None, settings: Settings) -> str | None:
    """Expose endpoint validation through the settings module's public contract."""
    return _endpoint(value, settings)


def credential_cipher(primary: str, secondary: str = "") -> MultiFernet:
    """Build a rotation-capable cipher: encrypt with ``primary``, decrypt with ``primary`` or ``secondary``.

    Raises ValueError/TypeError for a malformed key so callers can map it to their own failure.
    """
    keys = [primary, *([secondary] if secondary else [])]
    return MultiFernet([Fernet(key.encode()) for key in keys])


def _cipher(settings: Settings) -> MultiFernet:
    """Create the configured credential cipher (rotation key honoured for decrypt) or raise HTTP 503."""
    try:
        return credential_cipher(
            settings.ai_credential_encryption_key.get_secret_value(),
            settings.ai_credential_encryption_key_secondary.get_secret_value(),
        )
    except (ValueError, TypeError) as exc:
        raise UnavailableError(detail="AI credential encryption is unavailable") from exc


def _decrypt(ciphertext: str | None, settings: Settings) -> str:
    """Decrypt a saved credential, mapping corrupt ciphertext to HTTP 503."""
    if not ciphertext:
        return ""
    try:
        return _cipher(settings).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise UnavailableError(detail="Saved AI credentials cannot be decrypted") from exc


def scope_actor(scope: Scope) -> int:
    """Extract a typed actor; neither client dictionaries nor missing scope are authority."""
    if isinstance(scope, InternalJobScope):
        return scope.actor_user_id
    if isinstance(scope, WorkspaceContext):
        return scope.user_id
    raise UnauthorizedError(detail="Authentication required")


async def require_settings_scope(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    locked: bool = False,
    expected: AccessFence | None = None,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> AccessFence:
    """Admit only current owned-default Settings, before any domain/settings lock.

    Members cannot read Settings or secrets. Locked callers supply exact HTTP session
    refs when applicable and own commit/rollback; never retain these locks over I/O.
    """
    scope_actor(scope)
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError(detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("Explicit configured multi-workspace flag is required")
    if locked:
        return await lock_access_fence(
            session,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            expected=expected,
            auth_sessions=auth_sessions,
        )
    fence = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if expected is not None and fence != expected:
        raise ConflictError(detail="Workspace access fence changed")
    return fence


async def require_preferences_account(
    session: AsyncSession,
    *,
    actor_user_id: int,
    multi_workspace_enabled: bool,
    locked: bool = False,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> None:
    """Require actual active account without selected-workspace dependency or owner1 fallback.

    Locked mutation starts auth admission before preference rows; transaction completion
    belongs to caller. An arbitrary actor argument is not HTTP authority.
    """
    if (
        type(actor_user_id) is not int
        or actor_user_id <= 0
        or type(multi_workspace_enabled) is not bool
    ):
        raise ValueError("Positive actor and explicit configured feature flag are required")
    if locked:
        accounts = await lock_account_admission(
            session,
            (actor_user_id,),
            actor_user_id=actor_user_id,
            multi_workspace_enabled=multi_workspace_enabled,
            auth_sessions=auth_sessions,
        )
        account = accounts.get(actor_user_id)
    else:
        account = await get_active_account(
            session, actor_user_id, multi_workspace_enabled=multi_workspace_enabled
        )
    if account is None:
        raise UnauthorizedError(detail="Authentication required")


async def _row(
    session: AsyncSession, *, scope: Scope, locked: bool = False
) -> AISettingsRecord | None:
    """Read matching workspace+owner after admission; refresh identity map and optionally lock."""
    query = select(AISettingsRecord).where(
        AISettingsRecord.owner_id == scope_actor(scope),
        AISettingsRecord.workspace_id == scope.workspace_id,
    )
    if locked:
        query = query.with_for_update()
    return await session.scalar(query.execution_options(populate_existing=True))


def _privacy(
    raw: dict[str, object] | None, destinations: list[str], web_destinations: list[str]
) -> PrivacySettings:
    """Project explicit consent onto each remote provider's validated destination.

    ``destinations`` are the non-local provider destinations; a grant needs the flag and an exact list entry.
    Web search is independent of missing/denied providers; never emit a gateway destination or None in its allowlist.
    """
    raw = raw or {}

    def granted(key: str, enabled: str) -> list[str]:
        """Destinations that are enabled and exactly listed in this grant."""
        values = raw.get(key)
        if not raw.get(enabled) or not isinstance(values, list):
            return []
        return [d for d in dict.fromkeys(destinations) if d in values]

    reasoning = granted("reasoning_destinations", "allow_remote_reasoning")
    embeddings = granted("embedding_destinations", "allow_remote_embeddings")
    web_values = raw.get("web_search_destinations")
    web_search = (
        [d for d in dict.fromkeys(web_destinations) if d in web_values]
        if raw.get("allow_remote_web_search") and isinstance(web_values, list)
        else []
    )
    return PrivacySettings(
        allow_remote_reasoning=bool(reasoning),
        allow_remote_embeddings=bool(embeddings),
        allow_remote_web_search=bool(web_search),
        allow_widget_remote_media=bool(
            raw.get("allow_widget_remote_media")
        ),  # plain consent: no provider destination to bind
        reasoning_destinations=reasoning,
        embedding_destinations=embeddings,
        web_search_destinations=web_search,
    )


def _identity(
    endpoint: str | None, credential: str, *, local: bool = False
) -> tuple[str, str | None]:
    """Derive non-secret gateway and destination identifiers from configuration.

    Remote destinations use the ``remote:`` prefix, local ones ``local:``.
    """
    destination = (
        f"{'local' if local else 'remote'}:{_fingerprint(endpoint or '')[:32]}"
        if endpoint
        else None
    )
    return _fingerprint(
        json.dumps((endpoint, _fingerprint(credential)), separators=(",", ":"))
    ), destination


def _stored_providers(row: AISettingsRecord) -> dict[str, dict[str, Any]]:
    """Stored provider records keyed by id."""
    return {pid: dict(rec) for pid, rec in (row.providers or {}).items()}


def _models(record: dict[str, Any]) -> tuple[ProviderModel, ...]:
    """Typed models of a stored provider; malformed entries are dropped."""
    out = []
    for item in record.get("models") or []:
        try:
            out.append(ProviderModel.model_validate(item))
        except ValueError:
            continue
    return tuple(out)


def _destination(record: dict[str, Any], settings: Settings) -> str | None:
    """Destination id of a stored provider without decrypting its key; None when unset or denied by policy."""
    try:
        endpoint = _endpoint(record.get("base_url"), settings)
    except (DomainError, HTTPException) as exc:
        if exc.status_code != 422:
            raise
        return None
    return _identity(endpoint, "", local=record.get("kind") in LOCAL_PROVIDER_KINDS)[1]


def _resolve_provider(record: dict[str, Any], settings: Settings) -> ProviderConfig:
    """Validate one stored provider against deployment policy and decrypt its key; `local` comes from the kind."""
    kind = record.get("kind", "custom")
    denied = False
    try:
        endpoint = _endpoint(record.get("base_url"), settings)
    except (DomainError, HTTPException) as exc:
        if exc.status_code != 422:
            raise
        endpoint, denied = None, True
    # Read-time migration: a legacy hosted row pointing at a non-vendor URL (e.g. a proxy) behaves as ``custom``;
    # URL, key and destination are untouched. The raw URL is used when policy denied the endpoint.
    kind = effective_provider_kind(kind, endpoint or record.get("base_url"))
    local = kind in LOCAL_PROVIDER_KINDS
    ciphertext = record.get("key_ciphertext")
    return ProviderConfig(
        kind=kind,
        label=record.get("label") or "",
        base_url=endpoint,
        policy_denied=denied,
        local=local,
        api_key="" if denied else _decrypt(ciphertext, settings),
        credential_configured=bool(ciphertext),
        destination_id=None if denied else _identity(endpoint, "", local=local)[1],
        models=_models(record),
    )


def resolve_draft_endpoint(
    config: AIExecutionConfig,
    base_url: str | None,
    kind: ProviderKind | None,
    provider_id: str | None,
    settings: Settings,
) -> tuple[str, ProviderKind]:
    """Policy-validated endpoint and effective kind for an unsaved connection draft.

    ``kind`` falls back to the stored provider's kind, then ``custom``. A hosted kind ignores the client ``base_url``:
    it uses the saved provider's URL verbatim when that provider has the same kind and an equivalent URL (so
    ``resolve_draft_provider``'s same-endpoint key fallback still matches), else the vendor's fixed endpoint. A
    self-hosted kind requires ``base_url``.

    Raises:
        HTTPException: 422 when a self-hosted draft has no URL or the endpoint violates deployment policy.
    """
    stored = config.providers.get(provider_id or DEFAULT_PROVIDER_ID)
    resolved: ProviderKind = kind or (stored.kind if stored else "custom")
    if resolved in HOSTED_PROVIDER_KINDS:
        if (
            stored is not None
            and stored.kind == resolved
            and hosted_endpoint_equivalent(resolved, stored.base_url)
        ):
            raw = stored.base_url
        else:
            raw = HOSTED_PROVIDER_ENDPOINTS[resolved]
    else:
        raw = base_url
    endpoint = _endpoint(raw, settings)
    if endpoint is None:
        raise ValidationFailedError(detail="Gateway endpoint is required")
    return endpoint, resolved


def resolve_draft_provider(
    config: AIExecutionConfig,
    endpoint: str,
    api_key: str,
    provider_id: str | None,
    kind: str | None,
) -> tuple[str, bool, str]:
    """Credential, locality and destination id for an unsaved connection draft.

    An empty key falls back to the stored provider's key only for that provider's own endpoint, so a draft
    cannot send a stored secret to another host.
    """
    stored = config.providers.get(provider_id or DEFAULT_PROVIDER_ID)
    local = kind in LOCAL_PROVIDER_KINDS if kind else bool(stored and stored.local)
    if not api_key and stored is not None and stored.base_url == endpoint:
        api_key = stored.api_key
    return api_key, local, _identity(endpoint, "", local=local)[1] or ""


def derive_for_mapping(
    config: AIExecutionConfig, mapping: ModelMapping | None
) -> AIExecutionConfig:
    """Return the config with the gateway-facing endpoint/key/destination of the mapping's provider.

    Pure. A missing or unknown provider resolves to an unconfigured endpoint, which never sends;
    a config carrying no providers (hand-built snapshot) is returned as is.
    """
    if not config.providers:
        return config
    pid = (mapping.provider_id if mapping else None) or DEFAULT_PROVIDER_ID
    provider = config.providers.get(pid)
    if provider is None and pid == DEFAULT_PROVIDER_ID:
        provider = next(
            iter(config.providers.values()), None
        )  # no "default" provider: use the first one
    if provider is None:
        provider = ProviderConfig(kind="custom", base_url=None, destination_id=None)
    return config.model_copy(
        update={
            "gateway_base_url": provider.base_url,
            "gateway_api_key": provider.api_key,
            "gateway_kind": provider.kind,
            "endpoint_destination_id": provider.destination_id,
            "gateway_credential_configured": provider.credential_configured,
            "endpoint_policy_denied": provider.policy_denied,
        }
    )


def mapping_for_override(config: AIExecutionConfig, provider_id: str, model: str) -> ModelMapping:
    """Build the chat mapping for a per-request provider/model override.

    Pure. Shared by the chat worker and the capability probe so both derive the identical capability
    cache key. ``version`` is carried only when the override is the chat role default (same provider
    and model); other overrides are version-less. Does not check the model is configured.
    """
    provider = config.providers.get(provider_id)
    default = config.defaults.resolve("chat")
    same_default = (
        default is not None
        and default.model == model
        and (default.provider_id or DEFAULT_PROVIDER_ID) == provider_id
    )
    return ModelMapping(
        model=model,
        provider_id=provider_id,
        version=default.version if same_default and default else None,
        destination="unknown" if provider is None or provider.local else "remote",
    )


def derive_for_role(config: AIExecutionConfig, role: str) -> AIExecutionConfig:
    """Return the config pointed at the provider serving ``role`` (``background`` falls back to brief, then chat).

    Pure; an unmapped role resolves like a missing provider, i.e. an endpoint that never sends.
    """
    return derive_for_mapping(config, config.defaults.resolve(role))


def _web_destination(endpoint: str | None) -> str | None:
    """Privacy destination id of a web-search endpoint."""
    return f"web-search:{_fingerprint(endpoint)[:32]}" if endpoint else None
