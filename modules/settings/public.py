from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import HTTPException
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from core.auth.public import get_active_account, lock_account_admission
from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.model_gateway.schemas import (
    DEFAULT_PROVIDER_ID,
    LOCAL_PROVIDER_KINDS,
    MAX_PROVIDERS,
    MODEL_ROLES,
    AIExecutionConfig,
    AISettingsRead,
    AISettingsUpdate,
    ModelDefaults,
    ModelMapping,
    PrivacySettings,
    ProviderConfig,
    ProviderModel,
    ProviderRead,
    WebSearchTarget,
)
from core.model_gateway.transport import approved_transport
from core.workspaces.public import (
    lock_access_fence,
    read_access_fence,
    resolve_workspace_owner_context,
)
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.settings.models import AISettingsRecord, OwnerPreferencesRecord

# Re-exported for the model_gateway routes (strict mypy forbids implicit re-export, hence the aliases).
from modules.settings.models import list_capabilities as list_capabilities  # noqa: PLC0414
from modules.settings.models import (
    list_model_test_results as list_model_test_results,  # noqa: PLC0414
)
from modules.settings.models import new_capability_result as new_capability_result  # noqa: PLC0414
from modules.settings.models import save_capability as save_capability  # noqa: PLC0414
from modules.settings.models import (
    save_model_test_result as save_model_test_result,  # noqa: PLC0414
)
from modules.settings.prompts import DEFAULT_SYSTEM_PROMPTS, MAX_PROMPT_CHARS
from modules.settings.schemas import (
    ModuleLifecycleRead,
    N8nRead,
    N8nUpdate,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
    SystemPromptEntry,
    SystemPromptsRead,
    SystemPromptsUpdate,
    TranslationSettingsRead,
    VizRouterRead,
    VizRouterUpdate,
    WebSearchEntryRead,
    WebSearchRead,
    WebSearchUpdate,
)

VIZ_TYPESAFE_DESTINATION = "typesafe:api.typesafe.ai"
VIZ_OPENROUTER_URL = "https://openrouter.ai/api/v1"
VIZ_CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
VIZ_CLOUDFLARE_ACCOUNT = re.compile(r"[0-9a-f]{32}")
VIZ_KEYED_KINDS = ("openrouter", "cloudflare")  # self-contained OpenAI-compatible routers with their own endpoint and key
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
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise HTTPException(status_code=422, detail="Invalid gateway endpoint")
    try:
        port = parts.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid gateway endpoint") from exc
    if port == 0:
        raise HTTPException(status_code=422, detail="Gateway endpoint port must be between 1 and 65535")
    is_ip = True
    try:
        parsed_address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        normalized_host = parts.hostname.encode("idna").decode("ascii").lower()
        is_ipv6 = is_ip = False
    else:
        if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.scope_id is not None:
            raise HTTPException(status_code=422, detail="Scoped IPv6 gateway endpoints are not allowed")
        normalized_host = parsed_address.compressed.lower()
        is_ipv6 = isinstance(parsed_address, ipaddress.IPv6Address)
    default_port = 443 if parts.scheme.lower() == "https" else 80
    authority_host = f"[{normalized_host}]" if is_ipv6 else normalized_host
    effective_port = port if port is not None else default_port
    policy_authority = authority_host if effective_port == default_port else f"{authority_host}:{effective_port}"
    effective_authority = f"{authority_host}:{effective_port}"
    # Cloud providers: https public hostnames pass when the operator opted in (transport still pins the checked IPs).
    public_ok = settings.ai_allow_public_providers and parts.scheme.lower() == "https" and not is_ip
    if (policy_authority not in settings.ai_allowed_endpoint_hosts
            and effective_authority not in settings.ai_allowed_endpoint_hosts and not public_ok):
        raise HTTPException(status_code=422, detail="Endpoint address is not allowed by deployment policy")
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
        return credential_cipher(settings.ai_credential_encryption_key.get_secret_value(),
                                 settings.ai_credential_encryption_key_secondary.get_secret_value())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="AI credential encryption is unavailable") from exc


def _decrypt(ciphertext: str | None, settings: Settings) -> str:
    """Decrypt a saved credential, mapping corrupt ciphertext to HTTP 503."""
    if not ciphertext:
        return ""
    try:
        return _cipher(settings).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Saved AI credentials cannot be decrypted") from exc


def scope_actor(scope: Scope) -> int:
    """Extract a typed actor; neither client dictionaries nor missing scope are authority."""
    if isinstance(scope, InternalJobScope):
        return scope.actor_user_id
    if isinstance(scope, WorkspaceContext):
        return scope.user_id
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_settings_scope(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    locked: bool = False, expected: AccessFence | None = None,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> AccessFence:
    """Admit only current owned-default Settings, before any domain/settings lock.

    Members cannot read Settings or secrets. Locked callers supply exact HTTP session
    refs when applicable and own commit/rollback; never retain these locks over I/O.
    """
    scope_actor(scope)
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("Explicit configured multi-workspace flag is required")
    if locked:
        return await lock_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                                       expected=expected, auth_sessions=auth_sessions)
    fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if expected is not None and fence != expected:
        raise HTTPException(status_code=409, detail="Workspace access fence changed")
    return fence


async def require_preferences_account(
    session: AsyncSession, *, actor_user_id: int, multi_workspace_enabled: bool,
    locked: bool = False, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> None:
    """Require actual active account without selected-workspace dependency or owner1 fallback.

    Locked mutation starts auth admission before preference rows; transaction completion
    belongs to caller. An arbitrary actor argument is not HTTP authority.
    """
    if type(actor_user_id) is not int or actor_user_id <= 0 or type(multi_workspace_enabled) is not bool:
        raise ValueError("Positive actor and explicit configured feature flag are required")
    if locked:
        accounts = await lock_account_admission(session, (actor_user_id,), actor_user_id=actor_user_id,
                                                multi_workspace_enabled=multi_workspace_enabled, auth_sessions=auth_sessions)
        account = accounts.get(actor_user_id)
    else:
        account = await get_active_account(session, actor_user_id, multi_workspace_enabled=multi_workspace_enabled)
    if account is None:
        raise HTTPException(status_code=401, detail="Authentication required")


async def _row(session: AsyncSession, *, scope: Scope, locked: bool = False) -> AISettingsRecord | None:
    """Read matching workspace+owner after admission; refresh identity map and optionally lock."""
    query = select(AISettingsRecord).where(AISettingsRecord.owner_id == scope_actor(scope),
                                          AISettingsRecord.workspace_id == scope.workspace_id)
    if locked:
        query = query.with_for_update()
    return await session.scalar(query.execution_options(populate_existing=True))


def _privacy(raw: dict[str, object] | None, destinations: list[str], web_destinations: list[str]) -> PrivacySettings:
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
    web_search = ([d for d in dict.fromkeys(web_destinations) if d in web_values]
                  if raw.get("allow_remote_web_search") and isinstance(web_values, list) else [])
    return PrivacySettings(
        allow_remote_reasoning=bool(reasoning), allow_remote_embeddings=bool(embeddings),
        allow_remote_web_search=bool(web_search),
        allow_widget_remote_media=bool(raw.get("allow_widget_remote_media")),  # plain consent: no provider destination to bind
        reasoning_destinations=reasoning, embedding_destinations=embeddings,
        web_search_destinations=web_search,
    )


def _identity(endpoint: str | None, credential: str, *, local: bool = False) -> tuple[str, str | None]:
    """Derive non-secret gateway and destination identifiers from configuration.

    Remote destinations use the ``remote:`` prefix, local ones ``local:``.
    """
    destination = f"{'local' if local else 'remote'}:{_fingerprint(endpoint or '')[:32]}" if endpoint else None
    return _fingerprint(json.dumps((endpoint, _fingerprint(credential)), separators=(",", ":"))), destination


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
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        return None
    return _identity(endpoint, "", local=record.get("kind") in LOCAL_PROVIDER_KINDS)[1]


def _resolve_provider(record: dict[str, Any], settings: Settings) -> ProviderConfig:
    """Validate one stored provider against deployment policy and decrypt its key; `local` comes from the kind."""
    kind = record.get("kind", "custom")
    local = kind in LOCAL_PROVIDER_KINDS
    denied = False
    try:
        endpoint = _endpoint(record.get("base_url"), settings)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        endpoint, denied = None, True
    ciphertext = record.get("key_ciphertext")
    return ProviderConfig(
        kind=kind, label=record.get("label") or "", base_url=endpoint, policy_denied=denied, local=local,
        api_key="" if denied else _decrypt(ciphertext, settings), credential_configured=bool(ciphertext),
        destination_id=None if denied else _identity(endpoint, "", local=local)[1], models=_models(record))


def resolve_draft_provider(
    config: AIExecutionConfig, endpoint: str, api_key: str, provider_id: str | None, kind: str | None,
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


def derive_for_mapping(config: AIExecutionConfig, mapping: ModelMapping | None) -> AIExecutionConfig:
    """Return the config with the gateway-facing endpoint/key/destination of the mapping's provider.

    Pure. A missing or unknown provider resolves to an unconfigured endpoint, which never sends;
    a config carrying no providers (hand-built snapshot) is returned as is.
    """
    if not config.providers:
        return config
    pid = (mapping.provider_id if mapping else None) or DEFAULT_PROVIDER_ID
    provider = config.providers.get(pid)
    if provider is None and pid == DEFAULT_PROVIDER_ID:
        provider = next(iter(config.providers.values()), None)  # no "default" provider: use the first one
    if provider is None:
        provider = ProviderConfig(kind="custom", base_url=None, destination_id=None)
    return config.model_copy(update={
        "gateway_base_url": provider.base_url, "gateway_api_key": provider.api_key,
        "endpoint_destination_id": provider.destination_id, "gateway_credential_configured": provider.credential_configured,
        "endpoint_policy_denied": provider.policy_denied})


def mapping_for_override(config: AIExecutionConfig, provider_id: str, model: str) -> ModelMapping:
    """Build the chat mapping for a per-request provider/model override.

    Pure. Shared by the chat worker and the capability probe so both derive the identical capability
    cache key. ``version`` is carried only when the override is the chat role default (same provider
    and model); other overrides are version-less. Does not check the model is configured.
    """
    provider = config.providers.get(provider_id)
    default = config.defaults.resolve("chat")
    same_default = (default is not None and default.model == model
                    and (default.provider_id or DEFAULT_PROVIDER_ID) == provider_id)
    return ModelMapping(
        model=model, provider_id=provider_id, version=default.version if same_default and default else None,
        destination="unknown" if provider is None or provider.local else "remote",
    )


def derive_for_role(config: AIExecutionConfig, role: str) -> AIExecutionConfig:
    """Return the config pointed at the provider serving ``role`` (``background`` falls back to brief, then chat).

    Pure; an unmapped role resolves like a missing provider, i.e. an endpoint that never sends.
    """
    return derive_for_mapping(config, config.defaults.resolve(role))


async def get_ai_execution_config(session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope) -> AIExecutionConfig:
    """Resolve one active owner workspace execution snapshot using actual settings feature flag.

    Enforces endpoint policy before returning credentials and scopes privacy
    consent to exact destinations. Explicit scope is mandatory; recheck snapshot under locks before send.
    The top-level endpoint fields mirror provider ``default``; use ``derive_for_role`` before building a gateway.
    """
    fence = await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled)
    row = await _row(session, scope=scope)
    if row is None:
        providers: dict[str, ProviderConfig] = {}
        defaults = ModelDefaults()
        privacy = PrivacySettings()
        revision, timeout = 1, 20
        system_prompts: dict[str, str] = {}
        web_provider, web_endpoint, web_credential, web_configured = "none", None, "", False
        web_fallbacks: tuple[WebSearchTarget, ...] = ()
    else:
        providers = {pid: _resolve_provider(rec, settings) for pid, rec in _stored_providers(row).items()}
        defaults = ModelDefaults.model_validate({key: value for key, value in (row.defaults or {}).items()
                                                 if key in MODEL_ROLES and value})
        fallback_targets = []
        for item in row.web_search_fallbacks or []:
            try:
                fallback_key = _decrypt(item.get("key_ciphertext"), settings)
            except HTTPException as exc:
                if exc.status_code != 503:
                    raise
                fallback_key = ""  # A bad fallback secret must not disable unrelated capabilities.
            fallback_targets.append(WebSearchTarget(provider=item["provider"], endpoint=item["endpoint"],
                                                    api_key=fallback_key, credential_configured=bool(item.get("key_ciphertext"))))
        web_fallbacks = tuple(fallback_targets)
        web_destinations = [d for endpoint in (row.web_search_endpoint, *(t.endpoint for t in web_fallbacks))
                            if (d := _web_destination(endpoint))]
        privacy = _privacy(row.privacy, [p.destination_id for p in providers.values() if p.destination_id and not p.local], web_destinations)
        revision, timeout = row.configuration_revision, row.request_timeout_seconds
        system_prompts = dict(row.system_prompts or {})
        web_provider, web_endpoint = row.web_search_provider, row.web_search_endpoint
        web_configured = bool(row.web_search_api_key_ciphertext)
        try:
            web_credential = _decrypt(row.web_search_api_key_ciphertext, settings)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            # Web search is an independent capability; a broken web-search key
            # must not block lexical or semantic retrieval.
            web_credential = ""
    default = providers.get(DEFAULT_PROVIDER_ID) or next(iter(providers.values()), None) or ProviderConfig(
        kind="custom", base_url=None, destination_id=None)
    # Labels are display-only and stay out of the identity; any kind/endpoint/key change rotates it.
    credential_identity = _fingerprint(json.dumps(
        {pid: (p.kind, p.base_url, _fingerprint(p.api_key), p.destination_id, p.policy_denied) for pid, p in sorted(providers.items())},
        separators=(",", ":")))
    identity = _fingerprint(json.dumps((str(scope.workspace_id), scope_actor(scope), fence.membership_revision,
        fence.configuration_revision, revision, credential_identity, defaults.model_dump(),
        privacy.model_dump(), web_provider, web_endpoint, _fingerprint(web_credential),
        [(t.provider, t.endpoint, _fingerprint(t.api_key)) for t in web_fallbacks], tuple(sorted(settings.ai_allowed_endpoint_hosts)),
        tuple(sorted(settings.ai_allowed_endpoint_cidrs)), _fingerprint(settings.ai_credential_encryption_key.get_secret_value())),
        sort_keys=True, separators=(",", ":"), default=str))
    return AIExecutionConfig(
        workspace_id=scope.workspace_id, actor_user_id=scope_actor(scope), membership_revision=fence.membership_revision,
        access_configuration_revision=fence.configuration_revision,
        configuration_revision=revision, gateway_identity=identity, endpoint_destination_id=default.destination_id,
        endpoint_policy_denied=default.policy_denied, gateway_credential_configured=default.credential_configured,
        endpoint_allowed_cidrs=tuple(settings.ai_allowed_endpoint_cidrs), allow_public_providers=settings.ai_allow_public_providers,
        gateway_base_url=default.base_url, gateway_api_key=default.api_key, defaults=defaults, privacy=privacy,
        request_timeout_seconds=timeout,
        web_search_provider=web_provider, web_search_endpoint=web_endpoint, web_search_api_key=web_credential,
        web_search_credential_configured=web_configured, web_search_fallbacks=web_fallbacks,
        system_prompts=system_prompts, providers=providers,
    )


def _read(config: AIExecutionConfig) -> AISettingsRead:
    """Project execution settings into the API response without secret values."""
    return AISettingsRead(
        configuration_revision=config.configuration_revision,
        endpoint_destination_id=config.endpoint_destination_id,
        endpoint_policy_denied=config.endpoint_policy_denied,
        providers=[ProviderRead(id=pid, kind=p.kind, label=p.label, base_url=p.base_url, destination_id=p.destination_id,
                                credential_configured=p.credential_configured, local=p.local, policy_denied=p.policy_denied,
                                models=list(p.models))
                   for pid, p in config.providers.items()],
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        web_search_destination_id=(f"web-search:{_fingerprint(config.web_search_endpoint or '')[:32]}" if config.web_search_endpoint else None),
        web_search_credential_configured=config.web_search_credential_configured,
        defaults=config.defaults, capabilities=[], privacy=config.privacy,
        request_timeout_seconds=config.request_timeout_seconds,
    )


async def check_ai_execution_config(
    session: AsyncSession, settings: Settings, redis: Redis | None, *, scope: Scope,
    expected: AIExecutionConfig, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> None:
    """Lock current identity/settings and reject a changed execution snapshot before send/publish.

    Exact HTTP session refs are mandatory at HTTP callers. Workers supply their admitted
    durable scope and separately validate resource/claim/provenance. Caller must rollback
    or close this short transaction before network, then reacquire for publication.
    No transport, commit or ambient scope occurs here.
    """
    fence = AccessFence(workspace_id=expected.workspace_id, user_id=expected.actor_user_id,
                        membership_revision=expected.membership_revision,
                        configuration_revision=expected.access_configuration_revision)
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, expected=fence, auth_sessions=auth_sessions)
    await _row(session, scope=scope, locked=True)
    latest = await get_ai_execution_config(session, settings, redis, scope=scope)
    if latest.gateway_identity != expected.gateway_identity or latest.configuration_revision != expected.configuration_revision:
        raise HTTPException(status_code=409, detail="AI settings changed; prepare a fresh request")


async def lock_ai_settings_for_share(session: AsyncSession, *, scope: Scope) -> bool:
    """Share-lock the scoped AI settings row until the caller's transaction ends.

    Blocks ``save_ai_settings`` (row ``FOR UPDATE``) so a consent/provider/endpoint/key read in the same
    transaction cannot be revoked before the caller releases it. A leaf lock: savers lock nothing else.
    The caller already holds the access fence. Returns False when no row exists.
    """
    return (await session.scalar(
        select(AISettingsRecord.owner_id).where(
            AISettingsRecord.owner_id == scope_actor(scope), AISettingsRecord.workspace_id == scope.workspace_id,
        ).with_for_update(read=True)
    )) is not None


async def read_ai_settings(session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope) -> AISettingsRead:
    """Return owner-only explicit workspace settings projection without plaintext secrets."""
    return _read(await get_ai_execution_config(session, settings, redis, scope=scope))


def _prompts_read(revision: int, overrides: dict[str, str]) -> SystemPromptsRead:
    """Project overrides over code defaults for the prompts page."""
    return SystemPromptsRead(configuration_revision=revision, prompts=[
        SystemPromptEntry(key=key, value=overrides.get(key) or default, default=default, customized=key in overrides)
        for key, default in DEFAULT_SYSTEM_PROMPTS.items()])


async def read_system_prompts(session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope) -> SystemPromptsRead:
    """Return effective system prompts for the owner workspace."""
    config = await get_ai_execution_config(session, settings, redis, scope=scope)
    return _prompts_read(config.configuration_revision, config.system_prompts)


async def save_system_prompts(session: AsyncSession, update: SystemPromptsUpdate, settings: Settings, *, scope: Scope,
                              auth_sessions: tuple[AccountSessionRef, ...] = ()) -> SystemPromptsRead:
    """Revision-checked merge of prompt overrides; blank/None/default text resets a key.

    Bumps ``configuration_revision`` so in-flight runs holding an older snapshot fail their pre-send check.
    """
    if any(key not in DEFAULT_SYSTEM_PROMPTS for key in update.prompts):
        raise HTTPException(status_code=422, detail="Unknown system prompt")
    if any(value and len(value) > MAX_PROMPT_CHARS for value in update.prompts.values()):
        raise HTTPException(status_code=422, detail="System prompt too long")
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    await session.execute(insert(AISettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="AI settings changed; reload before saving")
    merged = dict(row.system_prompts or {})
    for key, value in update.prompts.items():
        text = (value or "").strip()
        if text and text != DEFAULT_SYSTEM_PROMPTS[key]:
            merged[key] = text
        else:
            merged.pop(key, None)
    row.system_prompts = merged
    row.configuration_revision += 1
    await session.flush()
    return _prompts_read(row.configuration_revision, merged)


def _kept_ciphertext(previous: dict[str, Any] | None, endpoint: str | None, settings: Settings) -> str | None:
    """Keep a stored key only while its provider endpoint is unchanged, so a saved key never reaches a new host."""
    if not previous:
        return None
    try:
        unchanged = _endpoint(previous.get("base_url"), settings) == endpoint
    except HTTPException:
        unchanged = False
    return previous.get("key_ciphertext") if unchanged else None


ROLE_MODEL_TYPE_LABEL = {
    "chat": "llm", "brief": "llm", "background": "background or llm", "embedding": "embedding",
    "reranker": "reranker", "vision": "vision-capable llm",
}


def role_accepts(role: str, model: ProviderModel) -> bool:
    """Whether a provider model's owner-assigned type can serve ``role``.

    chat/brief take ``llm``; background takes ``background`` or ``llm``; vision takes a vision-flagged ``llm``
    (or the legacy ``vision`` type); embedding and reranker need their own types.
    """
    if role in {"chat", "brief"}:
        return model.type == "llm"
    if role == "background":
        return model.type in {"background", "llm"}
    if role == "vision":
        return model.type == "vision" or (model.type == "llm" and model.vision)
    return model.type == role


async def save_ai_settings(session: AsyncSession, update: AISettingsUpdate, settings: Settings, *, scope: Scope,
                           auth_sessions: tuple[AccountSessionRef, ...] = ()) -> AISettingsRead:
    """Validate and persist an optimistic, revision-checked AI settings update.

    Locks admitted auth/workspace then matching owner settings, encrypts replacement credentials, and clears
    destination-bound privacy consent when its endpoint changes; the caller owns
    transaction commit/rollback. Chosen models must be discovered on their provider and match the role's model type
    (see ``role_accepts``); the embedding role must carry the probed vector dimensions and no other role may.
    """
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    # Composite schema ownership and scoped reread prevent borrowing another owner's PK.
    await session.execute(insert(AISettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="AI settings changed; reload before saving")
    stored = _stored_providers(row)
    providers: dict[str, dict[str, Any]]
    if update.providers is None:
        providers = dict(stored)  # providers are edited only by a full list
    else:
        if len(update.providers) > MAX_PROVIDERS:
            raise HTTPException(status_code=422, detail="Too many providers")
        if len({item.id for item in update.providers}) != len(update.providers):
            raise HTTPException(status_code=422, detail="Duplicate provider id")
        providers = {}
        for item in update.providers:
            endpoint = _endpoint(str(item.base_url) if item.base_url else None, settings)
            if endpoint is None:
                raise HTTPException(status_code=422, detail="Provider endpoint is required")
            ciphertext = _kept_ciphertext(stored.get(item.id), endpoint, settings)
            if item.credential_action == "replaced":
                if not item.api_key:
                    raise HTTPException(status_code=422, detail="Replacement provider credential is required")
                ciphertext = _cipher(settings).encrypt(item.api_key.encode()).decode()
            elif item.credential_action == "removed":
                ciphertext = None
            if len({m.id for m in item.models}) != len(item.models):
                raise HTTPException(status_code=422, detail="Duplicate model id")
            providers[item.id] = {"kind": item.kind, "label": item.label or item.id, "base_url": endpoint,
                                  "key_ciphertext": ciphertext, "local": item.kind in LOCAL_PROVIDER_KINDS,
                                  "models": [m.model_dump(mode="json") for m in item.models]}
    chosen_defaults = {role: mapping for role in MODEL_ROLES if (mapping := getattr(update.defaults, role)) is not None}
    for mapping in chosen_defaults.values():
        if mapping.provider_id is not None and mapping.provider_id not in providers:
            raise HTTPException(status_code=422, detail="Unknown model provider")
    for role, mapping in chosen_defaults.items():
        provider = providers.get(mapping.provider_id or DEFAULT_PROVIDER_ID) or (
            next(iter(providers.values()), None) if mapping.provider_id is None else None)
        # A chosen model must come from the provider's discovered list and match the role's model type.
        listed = {m.id: m for m in _models(provider)} if provider else {}
        if mapping.model and not listed:
            raise HTTPException(status_code=422, detail="Discover models for this provider first")
        if mapping.model and mapping.model not in listed:
            raise HTTPException(status_code=422, detail=f"Model {mapping.model} is not in the provider's model list")
        chosen = listed.get(mapping.model) if mapping.model else None
        if chosen is not None and not role_accepts(role, chosen):
            raise HTTPException(status_code=422, detail=f"Role {role} needs a {ROLE_MODEL_TYPE_LABEL[role]} model")
        if mapping.dimensions is not None and role != "embedding":
            raise HTTPException(status_code=422, detail="Dimensions apply only to the embedding role")
        if mapping.reasoning_effort != "off" and role != "chat":
            raise HTTPException(status_code=422, detail="Reasoning effort applies only to the chat role")
        if role == "embedding" and mapping.model and mapping.dimensions is None:
            raise HTTPException(status_code=422, detail="Embedding dimensions required; run the embeddings probe")
    next_web_endpoint = _endpoint(str(update.web_search_endpoint) if update.web_search_endpoint else None, settings)
    if (update.web_search_provider != "none" and next_web_endpoint
            and any(item.get("endpoint") == next_web_endpoint for item in (row.web_search_fallbacks or []))):
        raise HTTPException(status_code=422, detail="Web search endpoints must be unique")
    current_web_destination = f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}" if row.web_search_endpoint else None
    next_web_destination = f"web-search:{_fingerprint(next_web_endpoint or '')[:32]}" if next_web_endpoint else None
    privacy = update.privacy.model_dump(mode="json")
    # Consent is bound to each remote provider's destination: a new provider or a changed endpoint/kind never inherits it,
    # and removed providers drop out.
    grants: dict[str, list[str]] = {"reasoning_destinations": [], "embedding_destinations": []}
    for pid, record in providers.items():
        destination = _destination(record, settings)
        unchanged = pid in stored and _destination(stored[pid], settings) == destination
        if not destination or record["kind"] in LOCAL_PROVIDER_KINDS or not unchanged:
            continue
        for enabled, key in (("allow_remote_reasoning", "reasoning_destinations"), ("allow_remote_embeddings", "embedding_destinations")):
            if privacy[enabled] and destination in privacy[key]:
                grants[key].append(destination)
    for enabled, key in (("allow_remote_reasoning", "reasoning_destinations"), ("allow_remote_embeddings", "embedding_destinations")):
        privacy[key] = list(dict.fromkeys(grants[key]))
        privacy[enabled] = bool(privacy[key])
    # Existing fallback grants survive model saves but cannot be created through this legacy API.
    prior_web = (row.privacy or {}).get("web_search_destinations", [])
    prior_web = prior_web if isinstance(prior_web, list) else []
    fallback_destinations = [_web_destination(item.get("endpoint")) for item in (row.web_search_fallbacks or [])]
    web_grants = [d for d in fallback_destinations if d and d in prior_web and d in privacy["web_search_destinations"]]
    if (privacy["allow_remote_web_search"] and current_web_destination == next_web_destination
            and next_web_destination and update.web_search_provider != "none"):
        web_grants.insert(0, next_web_destination)
    privacy["web_search_destinations"] = list(dict.fromkeys(web_grants)) if privacy["allow_remote_web_search"] else []
    privacy["allow_remote_web_search"] = bool(privacy["web_search_destinations"])
    row.providers = providers
    row.privacy = privacy
    row.defaults = {role: mapping.model_dump(mode="json") for role, mapping in chosen_defaults.items()}
    row.web_search_provider = update.web_search_provider
    if current_web_destination != next_web_destination:
        row.web_search_api_key_ciphertext = None  # Never reuse a key at a changed destination.
    row.web_search_endpoint = next_web_endpoint
    if update.web_search_credential_action == "replaced":
        if not update.web_search_api_key:
            raise HTTPException(status_code=422, detail="Replacement search credential is required")
        row.web_search_api_key_ciphertext = _cipher(settings).encrypt(update.web_search_api_key.encode()).decode()
    elif update.web_search_credential_action == "removed":
        row.web_search_api_key_ciphertext = None
    row.request_timeout_seconds = update.request_timeout_seconds
    row.configuration_revision += 1
    await session.flush()
    return await read_ai_settings(session, settings, scope=scope)


def _web_destination(endpoint: str | None) -> str | None:
    """Privacy destination id of a web-search endpoint."""
    return f"web-search:{_fingerprint(endpoint)[:32]}" if endpoint else None


async def read_web_search(session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope) -> WebSearchRead:
    """Return ordered owner search destinations without exposing credential values."""
    config = await get_ai_execution_config(session, settings, redis, scope=scope)
    targets = []
    if config.web_search_provider != "none" and config.web_search_endpoint:
        targets.append(WebSearchEntryRead(provider=config.web_search_provider, endpoint=config.web_search_endpoint,
                                          credential_configured=config.web_search_credential_configured,
                                          destination_id=_web_destination(config.web_search_endpoint)))
    targets.extend(WebSearchEntryRead(provider=t.provider, endpoint=t.endpoint,
                                      credential_configured=t.credential_configured,
                                      destination_id=_web_destination(t.endpoint)) for t in config.web_search_fallbacks)
    return WebSearchRead(configuration_revision=config.configuration_revision, providers=targets,
                         allow_remote_web_search=config.privacy.allow_remote_web_search)


async def save_web_search(session: AsyncSession, update: WebSearchUpdate, settings: Settings, *, scope: Scope,
                          auth_sessions: tuple[AccountSessionRef, ...] = ()) -> WebSearchRead:
    """Replace ordered search targets and destination-bound consent under the owner revision fence.

    Keys are retained only for the same normalized endpoint, regardless of ordering; new keys are encrypted.
    The caller owns the transaction, and duplicate endpoints and missing required keys are rejected.
    """
    endpoints = [_endpoint(str(item.endpoint), settings) for item in update.providers]
    if any(endpoint is None for endpoint in endpoints) or len(set(endpoints)) != len(endpoints):
        raise HTTPException(status_code=422, detail="Web search endpoints must be present and unique")
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    await session.execute(insert(AISettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="AI settings changed; reload before saving")
    stored = {row.web_search_endpoint: row.web_search_api_key_ciphertext}
    stored.update({item.get("endpoint"): item.get("key_ciphertext") for item in (row.web_search_fallbacks or [])})
    records = []
    for item, endpoint in zip(update.providers, endpoints, strict=True):
        ciphertext = stored.get(endpoint)
        if item.credential_action == "replaced":
            if not item.api_key:
                raise HTTPException(status_code=422, detail="Replacement search credential is required")
            ciphertext = _cipher(settings).encrypt(item.api_key.encode()).decode()
        elif item.credential_action == "removed":
            ciphertext = None
        if item.provider in ("tavily", "brave", "omniroute") and not ciphertext:
            raise HTTPException(status_code=422, detail="Search credential is required for this provider")
        records.append({"provider": item.provider, "endpoint": endpoint, "key_ciphertext": ciphertext})
    if records:
        row.web_search_provider = update.providers[0].provider
        row.web_search_endpoint = endpoints[0]
        row.web_search_api_key_ciphertext = records[0]["key_ciphertext"]
    else:
        row.web_search_provider, row.web_search_endpoint, row.web_search_api_key_ciphertext = "none", None, None
    row.web_search_fallbacks = records[1:]
    destinations = [_web_destination(endpoint) for endpoint in endpoints]
    consent = bool(update.allow_remote_web_search and destinations)
    row.privacy = {**(row.privacy or {}), "allow_remote_web_search": consent,
                   "web_search_destinations": destinations if consent else []}
    row.configuration_revision += 1
    await session.flush()
    return await read_web_search(session, settings, scope=scope)


class N8nResolved(BaseModel):
    """Effective n8n connection: stored value first, ``N8N_*`` environment otherwise. Secrets stay out of repr."""
    service_url: str
    api_key: str = Field(default="", repr=False)
    webhook_token: str = Field(default="", repr=False)
    service_url_source: str = "environment"
    api_key_source: str = "none"
    webhook_token_source: str = "none"
    enabled: bool = True


async def _n8n_stored(session: AsyncSession, workspace_id: UUID | None) -> dict[str, Any]:
    """Read the stored n8n JSON: the workspace row, or for host workers the first owner row with n8n settings.

    Plain read, no admission and no lock; an absent row yields ``{}``.
    """
    query = select(AISettingsRecord.n8n)
    if workspace_id is not None:
        query = query.where(AISettingsRecord.workspace_id == workspace_id)
    else:
        # Workers: deterministic pick among owners with n8n settings - the owner's default workspace first,
        # then by workspace id, then owner id. ponytail: single n8n per host; per-workspace worker routing if needed.
        query = (query.outerjoin(Owner, Owner.id == AISettingsRecord.owner_id)
                 .where(AISettingsRecord.n8n != text("'{}'::jsonb"))
                 .order_by((AISettingsRecord.workspace_id == Owner.default_workspace_id).desc(),
                           AISettingsRecord.workspace_id, AISettingsRecord.owner_id))
    stored: dict[str, Any] = await session.scalar(query.limit(1)) or {}
    return stored


async def n8n_enabled(session: AsyncSession, *, workspace_id: UUID | None = None) -> bool:
    """Return the n8n kill switch (absent flag means enabled) without decrypting any secret.

    Plain read for ingress guards that have no ``Settings`` at hand; ``workspace_id`` None follows the
    worker pick used by ``resolve_n8n``.
    """
    return bool((await _n8n_stored(session, workspace_id)).get("enabled", True))


async def resolve_n8n(session: AsyncSession, settings: Settings, *, workspace_id: UUID | None = None) -> N8nResolved:
    """Resolve n8n URL, API key and webhook token: owner-saved settings, then environment.

    Each field falls back independently. Request paths pass their ``workspace_id``; host-level workers
    (no workspace) read the first owner row that has n8n settings. A ciphertext that cannot be decrypted
    counts as unset. Plain read: no admission, no lock.
    """
    stored = await _n8n_stored(session, workspace_id)

    url = stored.get("service_url")
    # Env secrets belong to the env URL only; never send them to an owner-chosen host.
    env_ok = not url or url.rstrip("/") == str(settings.n8n_service_url).rstrip("/")

    def pick(name: str, env: str) -> tuple[str, str]:
        ciphertext = stored.get(f"{name}_ciphertext")
        if ciphertext:
            try:
                return _decrypt(ciphertext, settings), "database"
            except HTTPException:
                pass
        return (env, "environment") if env and env_ok else ("", "none")

    key, key_source = pick("api_key", settings.n8n_api_key.get_secret_value())
    token, token_source = pick("webhook_token", settings.n8n_webhook_token.get_secret_value())
    return N8nResolved(
        service_url=url or str(settings.n8n_service_url), service_url_source="database" if url else "environment",
        api_key=key, api_key_source=key_source, webhook_token=token, webhook_token_source=token_source,
        enabled=bool(stored.get("enabled", True)))


def _n8n_revision(row: Any) -> int:
    """n8n optimistic-concurrency revision, kept inside the n8n JSON (absent = 1)."""
    return int((getattr(row, "n8n", None) or {}).get("revision", 1)) if row else 1


def n8n_service_url(value: Any, settings: Settings) -> str | None:
    """Validate an owner-chosen n8n URL: the env URL's host is always allowed, others follow endpoint policy."""
    if not value:
        return None
    url = str(value).rstrip("/")
    if urlsplit(url).netloc.lower() == urlsplit(str(settings.n8n_service_url)).netloc.lower():
        return url
    return _endpoint(url, settings)


def n8n_http_client(url: str, settings: Settings, *, timeout: float) -> httpx.AsyncClient:
    """HTTP client for n8n: DNS-pinned to approved addresses unless ``url`` is the operator's ``N8N_SERVICE_URL`` host.

    Owner-chosen hosts (allowlisted or public-provider https) are resolved once per request, every answer is checked
    against ``AI_ALLOWED_ENDPOINT_CIDRS`` (or public when opted in) and the checked IP is dialled, so DNS rebinding
    cannot reach internal hosts. No proxies and no redirects. Raises EndpointNetworkPolicyError (fail closed) when
    no address policy is configured.
    """
    if urlsplit(url).netloc.lower() == urlsplit(str(settings.n8n_service_url)).netloc.lower():
        return httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False)
    transport = approved_transport(url, settings.ai_allowed_endpoint_cidrs, allow_global=settings.ai_allow_public_providers)
    return httpx.AsyncClient(transport=transport, timeout=timeout, trust_env=False, follow_redirects=False)


async def read_n8n(session: AsyncSession, settings: Settings, *, scope: Scope) -> N8nRead:
    """Return n8n settings for the owner workspace: configured flags and sources, never secrets."""
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled)
    row = await _row(session, scope=scope)
    from modules.connectors.public import n8n_callback_base

    resolved = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    return N8nRead(
        configuration_revision=_n8n_revision(row), service_url=resolved.service_url,
        api_key_configured=bool(resolved.api_key), webhook_token_configured=bool(resolved.webhook_token),
        service_url_source=resolved.service_url_source, api_key_source=resolved.api_key_source,
        webhook_token_source=resolved.webhook_token_source, enabled=resolved.enabled,
        callback_base_url=n8n_callback_base())


async def save_n8n(session: AsyncSession, update: N8nUpdate, settings: Settings, *, scope: Scope,
                   auth_sessions: tuple[AccountSessionRef, ...] = ()) -> N8nRead:
    """Revision-checked replace of the stored n8n URL; secrets change only by their ``*_action``.

    ``service_url`` None clears the stored URL. ``enabled`` None keeps the stored kill switch (absent =
    enabled); it changes under the same revision check and never touches n8n workflows. Secrets are
    Fernet-encrypted like AI credentials.
    Uses its own ``revision`` inside the n8n JSON and never bumps ``configuration_revision``
    (that would fail in-flight AI runs).
    """
    service_url = n8n_service_url(update.service_url, settings)
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    await session.execute(insert(AISettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if _n8n_revision(row) != update.expected_revision:
        raise HTTPException(status_code=409, detail="n8n settings changed; reload before saving")
    stored = dict(row.n8n or {})
    stored.pop("service_url", None)
    if service_url:
        stored["service_url"] = service_url
    for name, action, value in (("api_key", update.api_key_action, update.api_key),
                                ("webhook_token", update.webhook_token_action, update.webhook_token)):
        if action == "replaced":
            if not value:
                raise HTTPException(status_code=422, detail=f"Replacement n8n {name.replace('_', ' ')} is required")
            stored[f"{name}_ciphertext"] = _cipher(settings).encrypt(value.encode()).decode()
        elif action == "removed":
            stored.pop(f"{name}_ciphertext", None)
    if update.enabled is not None:
        stored["enabled"] = update.enabled
    stored["revision"] = _n8n_revision(row) + 1
    row.n8n = stored
    await session.flush()
    return await read_n8n(session, settings, scope=scope)


class VizRouterResolved(BaseModel):
    """Effective visualization router configuration. ``allow_remote`` is already destination-checked; key stays out of repr."""
    kind: str = "internal"
    provider_id: str | None = None
    model: str = ""
    mode: str = "json_schema"
    api: str = "chat"  # openrouter only: "decisions" selects the Decisions API (alpha); absent in old rows => chat
    typesafe_key: str = Field(default="", repr=False)
    base_url: str | None = None  # openrouter/cloudflare only: validated endpoint
    api_key: str = Field(default="", repr=False)  # openrouter/cloudflare only
    allow_remote: bool = False
    destination_id: str | None = None
    revision: int = 1


def _viz_stored(row: Any) -> dict[str, Any]:
    """Stored visualization router JSON of a settings row; empty (internal router) when absent."""
    return dict(getattr(row, "viz_router", None) or {}) if row else {}


def _viz_endpoint(stored: dict[str, Any], settings: Settings) -> str | None:
    """Validated base URL of a self-contained router kind (``openrouter`` fixed, ``cloudflare`` account or gateway URL).

    Raises HTTP 422 when the Cloudflare account id is malformed, the override is not https, or deployment endpoint
    policy (allowed hosts / public providers) denies the host. Other kinds have no endpoint of their own: None.
    """
    kind = stored.get("kind")
    if kind == "openrouter":
        url: str | None = VIZ_OPENROUTER_URL
    elif kind == "cloudflare":
        account = str(stored.get("account_id") or "")
        url = stored.get("gateway_url") or (
            VIZ_CLOUDFLARE_URL.format(account_id=account) if VIZ_CLOUDFLARE_ACCOUNT.fullmatch(account) else None)
        if url is None or not url.lower().startswith("https://"):
            raise HTTPException(status_code=422, detail="Cloudflare needs a 32-character account id or an https gateway URL")
    else:
        return None
    return _endpoint(url, settings)


def _viz_destination(stored: dict[str, Any], providers: dict[str, dict[str, Any]], settings: Settings) -> str | None:
    """Destination id the stored router would send to: TypeSafe host, own endpoint (openrouter/cloudflare) or the provider's.

    Own-endpoint kinds derive the id from the validated URL, so changing the Cloudflare account or gateway changes it
    and drops saved consent; a policy-denied endpoint has no destination (None).
    """
    if stored.get("kind") == "typesafe":
        return VIZ_TYPESAFE_DESTINATION
    if stored.get("kind") in VIZ_KEYED_KINDS:
        try:
            endpoint = _viz_endpoint(stored, settings)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            return None
        return _identity(endpoint, "")[1]
    if stored.get("kind") == "llm":
        record = providers.get(stored.get("provider_id") or DEFAULT_PROVIDER_ID)
        return _destination(record, settings) if record else None
    return None


async def resolve_viz_router(session: AsyncSession, settings: Settings, *, scope: Scope) -> VizRouterResolved:
    """Resolve the visualization router for the owner workspace; no row/JSON means the internal router.

    ``allow_remote`` is true only when the owner granted consent for the *current* destination (so editing the
    provider endpoint later revokes it) or the provider is local (no remote egress, so no consent is needed).
    An undecryptable TypeSafe/OpenRouter/Cloudflare key counts as unset; a policy-denied own endpoint has no base_url. Plain read: no lock.
    """
    row = await _row(session, scope=scope)
    stored = _viz_stored(row)
    kind = stored.get("kind", "internal")
    if row is None or kind not in {"llm", "typesafe", *VIZ_KEYED_KINDS}:
        return VizRouterResolved(revision=int(stored.get("revision", 1)))
    providers = _stored_providers(row)
    destination = _viz_destination(stored, providers, settings)
    record = providers.get(stored.get("provider_id") or DEFAULT_PROVIDER_ID) if kind == "llm" else None
    local = bool(record and record.get("kind") in LOCAL_PROVIDER_KINDS)
    consent = (bool(stored.get("allow_remote_visualization_routing")) and destination is not None
               and stored.get("consent_destination") == destination)
    key = ""
    ciphertext = stored.get(f"{kind}_key_ciphertext")
    if kind in {"typesafe", *VIZ_KEYED_KINDS} and ciphertext:
        try:
            key = _decrypt(ciphertext, settings)
        except HTTPException:
            key = ""
    base_url = None
    if kind in VIZ_KEYED_KINDS:
        try:
            base_url = _viz_endpoint(stored, settings)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
    return VizRouterResolved(
        kind=kind, provider_id=stored.get("provider_id"), model=stored.get("model", ""), mode=stored.get("mode", "json_schema"),
        api="decisions" if kind == "openrouter" and stored.get("api") == "decisions" else "chat",
        typesafe_key=key if kind == "typesafe" else "", base_url=base_url, api_key=key if kind in VIZ_KEYED_KINDS else "",
        allow_remote=consent or (local and destination is not None), destination_id=destination,
        revision=int(stored.get("revision", 1)))


def viz_decision_discovery_config(config: AIExecutionConfig, api_key: str, settings: Settings) -> AIExecutionConfig:
    """Pure-ish: execution config pointed at the fixed OpenRouter endpoint with ``api_key`` for model discovery.

    The endpoint goes through the same deployment host policy as a saved router (422 when denied), so discovery
    cannot reach a host the owner's policy forbids. The key is used for this call only.
    """
    endpoint = _endpoint(VIZ_OPENROUTER_URL, settings)
    return config.model_copy(update={
        "gateway_base_url": endpoint, "gateway_api_key": api_key,
        "endpoint_destination_id": _identity(endpoint, "")[1], "gateway_credential_configured": bool(api_key),
        "endpoint_policy_denied": endpoint is None})


def viz_router_gateway_config(config: AIExecutionConfig, resolved: VizRouterResolved) -> tuple[AIExecutionConfig, ModelMapping]:
    """Pure: the execution config pointed at the router's provider, plus the router's model mapping.

    ``derive_for_mapping`` selects the router provider's endpoint, key and destination; remote providers are
    marked ``destination="remote"`` for the gateway privacy policy. The router has no role slot in the defaults.
    Probe and runtime share a nonsecret cache identity bound to the base configuration and router selection,
    revision, API, endpoint and credential fingerprint; changing the router invalidates only its evidence.
    """
    # Router saves deliberately leave the base AI revision unchanged, so its evidence needs a separate identity.
    identity = _fingerprint(json.dumps((
        config.gateway_identity, resolved.model_dump(exclude={"typesafe_key", "api_key"}), _fingerprint(resolved.api_key),
    ), sort_keys=True, separators=(",", ":")))
    config = config.model_copy(update={"gateway_identity": identity})
    if resolved.kind in VIZ_KEYED_KINDS:
        # Self-contained kind: its own validated endpoint and key replace the provider's; never the owner's providers.
        own = ModelMapping(model=resolved.model, destination="remote")
        return config.model_copy(update={
            "gateway_base_url": resolved.base_url, "gateway_api_key": resolved.api_key,
            "endpoint_destination_id": resolved.destination_id, "gateway_credential_configured": bool(resolved.api_key),
            "endpoint_policy_denied": resolved.base_url is None}), own
    provider = config.providers.get(resolved.provider_id or DEFAULT_PROVIDER_ID)
    mapping = ModelMapping(model=resolved.model, provider_id=resolved.provider_id,
                           destination="unknown" if provider is None or provider.local else "remote")
    return derive_for_mapping(config, mapping), mapping


async def read_viz_router(session: AsyncSession, settings: Settings, *, scope: Scope) -> VizRouterRead:
    """Return the router settings projection for the owner workspace: key presence only, never the key."""
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled)
    row = await _row(session, scope=scope)
    resolved = await resolve_viz_router(session, settings, scope=scope)
    return VizRouterRead.model_validate({
        "configuration_revision": resolved.revision, "kind": resolved.kind, "provider_id": resolved.provider_id,
        "model": resolved.model, "mode": resolved.mode, "api": resolved.api,
        "typesafe_key_configured": bool(_viz_stored(row).get("typesafe_key_ciphertext")),
        "openrouter_key_configured": bool(_viz_stored(row).get("openrouter_key_ciphertext")),
        "cloudflare_key_configured": bool(_viz_stored(row).get("cloudflare_key_ciphertext")),
        "account_id": _viz_stored(row).get("account_id"), "gateway_url": _viz_stored(row).get("gateway_url"),
        "allow_remote_visualization_routing": resolved.allow_remote, "destination_id": resolved.destination_id})


async def save_viz_router(session: AsyncSession, update: VizRouterUpdate, settings: Settings, *, scope: Scope,
                          auth_sessions: tuple[AccountSessionRef, ...] = ()) -> VizRouterRead:
    """Revision-checked replace of the visualization router settings (own ``revision`` inside the JSON).

    ``llm`` needs an existing provider and a model from that provider's discovered ``decision`` (or legacy ``llm``) list; ``typesafe``, ``openrouter`` and ``cloudflare`` encrypt their own key like AI credentials; openrouter/cloudflare also need a
    model and key and validate their endpoint against deployment policy. Consent is stored bound to the destination chosen in this save and is
    dropped for ``internal``. Never bumps ``configuration_revision`` (that would fail in-flight AI runs).
    """
    await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled,
                                 locked=True, auth_sessions=auth_sessions)
    await session.execute(insert(AISettingsRecord).values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    previous = _viz_stored(row)
    if int(previous.get("revision", 1)) != update.expected_revision:
        raise HTTPException(status_code=409, detail="Visualization router settings changed; reload before saving")
    providers = _stored_providers(row)
    stored: dict[str, Any] = {"kind": update.kind, "revision": int(previous.get("revision", 1)) + 1}
    if update.kind == "llm":
        pid = update.provider_id or DEFAULT_PROVIDER_ID
        record = providers.get(pid)
        if record is None:
            raise HTTPException(status_code=422, detail="Unknown model provider")
        if not any(m.id == update.model and m.type in {"decision", "llm"} for m in _models(record)):
            raise HTTPException(status_code=422, detail="Choose a decision or language model discovered on this provider")
        stored.update(provider_id=pid, model=update.model, mode=update.mode)
    if update.kind in VIZ_KEYED_KINDS:
        if not update.model.strip():
            raise HTTPException(status_code=422, detail="A router model is required")
        stored.update(model=update.model.strip(), mode=update.mode)
        if update.kind == "openrouter" and update.api == "decisions":
            stored["api"] = "decisions"  # only openrouter has the Decisions API; omitted => chat
        if update.kind == "cloudflare":
            stored.update(account_id=(update.account_id or "").strip().lower() or None,
                          gateway_url=(update.gateway_url or "").strip() or None)
        _viz_endpoint(stored, settings)  # 422 on bad account id / non-https / deployment endpoint policy
    ciphertexts = {name: previous.get(f"{name}_key_ciphertext") for name in ("typesafe", *VIZ_KEYED_KINDS)}
    if update.kind == "typesafe":
        action, value, label = update.typesafe_key_action, update.typesafe_key, "TypeSafe"
    else:
        action, value, label = update.api_key_action, update.api_key, update.kind
    if update.kind in {"typesafe", *VIZ_KEYED_KINDS}:
        if action == "replaced":
            if not value:
                raise HTTPException(status_code=422, detail=f"Replacement {label} key is required")
            ciphertexts[update.kind] = _cipher(settings).encrypt(value.encode()).decode()
        elif action == "removed":
            ciphertexts[update.kind] = None
    if update.kind in VIZ_KEYED_KINDS and not ciphertexts[update.kind]:
        raise HTTPException(status_code=422, detail=f"A {label} API key is required")
    for name, ciphertext in ciphertexts.items():
        if ciphertext:  # per-kind: a key is never reused for another kind's host; only "removed" drops it
            stored[f"{name}_key_ciphertext"] = ciphertext
    destination = _viz_destination(stored, providers, settings)
    if update.kind != "internal" and update.allow_remote_visualization_routing and destination:
        stored["allow_remote_visualization_routing"] = True
        stored["consent_destination"] = destination
    row.viz_router = stored
    await session.flush()
    return await read_viz_router(session, settings, scope=scope)


async def read_owner_preferences(session: AsyncSession, *, actor_user_id: int, multi_workspace_enabled: bool) -> OwnerPreferencesRead:
    """Return active account preferences/defaults; selected workspace is irrelevant."""
    await require_preferences_account(session, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == actor_user_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return OwnerPreferencesRead(
            configuration_revision=1,
            persisted=False,
            theme="system",
            locale="en-us",
            timezone="Asia/Ho_Chi_Minh",
        )
    return OwnerPreferencesRead(
        configuration_revision=row.configuration_revision,
        persisted=True,
        theme=row.theme,
        locale=row.locale,
        timezone=row.timezone,
    )


async def read_module_availability(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> ModuleLifecycleRead:
    """Return owner-only scoped module DTO with actual configured gate; never mutate process state."""
    from modules.settings.lifecycle import read_module_lifecycle

    return await read_module_lifecycle(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def read_retention_settings(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> RetentionSettingsRead:
    """Return admitted workspace retention DTO; workers must bind the same scope to cleanup."""
    from modules.settings.lifecycle import read_retention_settings as _read_retention_settings

    return await _read_retention_settings(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def module_is_enabled(session: AsyncSession, module_id: str, *, scope: Scope, multi_workspace_enabled: bool) -> bool:
    """Resolve admitted owner workspace availability for jobs/tools, never global settings."""
    from modules.settings.lifecycle import module_is_enabled as _module_is_enabled

    return await _module_is_enabled(session, module_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def module_dependency(module_id: str) -> Callable[..., Awaitable[None]]:
    """Build scoped availability dependency; domain owner separately enforces resource grants."""
    from modules.settings.lifecycle import module_dependency as _module_dependency

    return _module_dependency(module_id)


async def save_owner_preferences(
    session: AsyncSession,
    update: OwnerPreferencesUpdate,
    *, actor_user_id: int, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> OwnerPreferencesRead:
    """Persist actual account preferences with exact-session auth admission and CAS.

    Creates then locks the actor row to serialize first writes; transaction
    completion remains the caller's responsibility.
    """
    await require_preferences_account(session, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled,
                                      locked=True, auth_sessions=auth_sessions)
    await session.execute(
        insert(OwnerPreferencesRecord)
        .values(owner_id=actor_user_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == actor_user_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        raise RuntimeError("Owner preferences singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="Preferences changed; reload before saving")
    row.theme = update.theme
    row.locale = update.locale
    row.timezone = update.timezone
    row.configuration_revision += 1
    await session.flush()
    return OwnerPreferencesRead(
        configuration_revision=row.configuration_revision,
        persisted=True,
        theme=row.theme,
        locale=row.locale,
        timezone=row.timezone,
    )


async def get_translation_settings(session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool) -> TranslationSettingsRead:
    """Workspace translation choice for T2/T3 consumers; member-safe, admission before query."""
    from modules.translations.public import (
        read_translation_settings,  # lazy: avoids a settings<->translations import cycle
    )
    return await read_translation_settings(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def read_ai_policy_fingerprint(
    session: AsyncSession, settings: Settings, redis: Redis | None, *, workspace_id: UUID, role: str,
) -> str | None:
    """Nonsecret digest of the workspace owner's AI execution policy for one role, or None when unusable.

    Members cannot read Settings, so the owner context is resolved internally. Covers endpoint identity,
    credential fingerprint, role mapping version, remote/privacy flags and AI settings revision.
    """
    owner = await resolve_workspace_owner_context(
        session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled)
    if owner is None:
        return None
    config = derive_for_role(await get_ai_execution_config(session, settings, redis, scope=owner), role)
    mapping = config.defaults.resolve(role)
    if mapping is None or config.gateway_base_url is None or config.endpoint_policy_denied:
        return None
    destination = config.endpoint_destination_id
    if destination is None:
        return None
    # Local providers need neither a credential nor remote consent.
    if not destination.startswith("local:") and not (config.gateway_credential_configured and config.privacy.allow_remote_reasoning):
        return None
    return hashlib.sha256(json.dumps(
        (destination, _fingerprint(config.gateway_api_key), role, mapping.model, mapping.version, mapping.destination,
         config.privacy.model_dump(mode="json"), config.configuration_revision),
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
