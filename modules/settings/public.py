from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.auth.public import get_active_account, lock_account_admission
from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.model_gateway.schemas import (
    DEFAULT_PROVIDER_ID,
    LOCAL_PROVIDER_KINDS,
    MAX_PROVIDERS,
    AIExecutionConfig,
    AISettingsRead,
    AISettingsUpdate,
    ModelMapping,
    PrivacySettings,
    ProviderConfig,
    ProviderRead,
)
from core.workspaces.public import (
    lock_access_fence,
    read_access_fence,
    resolve_workspace_owner_context,
)
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.backup.schemas import ActivityReceipt, AdmissionReceipt
from modules.settings.models import AISettingsRecord, OwnerPreferencesRecord, legacy_aliases
from modules.settings.models import list_capabilities as list_capabilities
from modules.settings.models import new_capability_result as new_capability_result
from modules.settings.models import save_capability as save_capability
from modules.settings.schemas import (
    ModuleLifecycleRead,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
    SystemPromptEntry,
    SystemPromptsRead,
    SystemPromptsUpdate,
    TranslationSettingsRead,
)
from modules.settings.prompts import DEFAULT_SYSTEM_PROMPTS, MAX_PROMPT_CHARS

ALIASES = ("reasoning-large", "reasoning-small", "fast", "embedding", "reranker", "vision", "local-private")


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


def _cipher(settings: Settings) -> Fernet:
    """Create the configured credential cipher or raise HTTP 503 if unavailable."""
    key = settings.ai_credential_encryption_key.get_secret_value()
    try:
        return Fernet(key.encode())
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


def _defaults(settings: Settings) -> tuple[str | None, str, dict[str, ModelMapping]]:
    """Read deployment-provided endpoint, key, and supported model defaults."""
    endpoint = str(settings.omniroute_base_url) if settings.omniroute_base_url else None
    aliases = {name: ModelMapping(model=model, destination="remote") for name, model in settings.omniroute_models.items() if name in ALIASES}
    return endpoint, settings.omniroute_api_key.get_secret_value(), aliases


def _privacy(raw: dict[str, object] | None, destinations: list[str], web_destination: str | None) -> PrivacySettings:
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
    web_search = bool(web_destination and raw.get("allow_remote_web_search")
                      and isinstance(web_values, list) and web_destination in web_values)
    return PrivacySettings(
        allow_remote_reasoning=bool(reasoning), allow_remote_embeddings=bool(embeddings),
        allow_remote_web_search=web_search,
        reasoning_destinations=reasoning, embedding_destinations=embeddings,
        web_search_destinations=[web_destination] if web_search and web_destination else [],
    )


def _identity(endpoint: str | None, credential: str, *, local: bool = False) -> tuple[str, str | None]:
    """Derive non-secret gateway and destination identifiers from configuration.

    Remote destinations keep the legacy ``omniroute:`` prefix so consent saved before multi-provider survives.
    """
    destination = f"{'local' if local else 'omniroute'}:{_fingerprint(endpoint or '')[:32]}" if endpoint else None
    return _fingerprint(json.dumps((endpoint, _fingerprint(credential)), separators=(",", ":"))), destination


def _stored_providers(row: AISettingsRecord) -> dict[str, dict[str, Any]]:
    """Stored provider records; synthesise ``default`` from the legacy columns while ``providers`` is empty."""
    if row.providers:
        return {pid: dict(rec) for pid, rec in row.providers.items()}
    return {DEFAULT_PROVIDER_ID: {"kind": "omniroute", "label": "Default", "base_url": row.omniroute_base_url,
                                  "key_ciphertext": row.omniroute_api_key_ciphertext, "local": False}}


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
        destination_id=None if denied else _identity(endpoint, "", local=local)[1])


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


def derive_for_alias(config: AIExecutionConfig, alias: str) -> AIExecutionConfig:
    """Return the config with the gateway-facing endpoint/key/destination of the alias's provider.

    Pure. A missing or unknown provider resolves to an unconfigured endpoint, which never sends;
    a config carrying no providers (hand-built snapshot) is returned as is.
    """
    if not config.providers:
        return config
    mapping = config.aliases.get(alias)
    provider = config.providers.get((mapping.provider_id if mapping else None) or DEFAULT_PROVIDER_ID)
    if provider is None:
        provider = ProviderConfig(kind="custom", base_url=None, destination_id=None)
    return config.model_copy(update={
        "omniroute_base_url": provider.base_url, "omniroute_api_key": provider.api_key,
        "endpoint_destination_id": provider.destination_id, "omniroute_credential_configured": provider.credential_configured,
        "endpoint_policy_denied": provider.policy_denied})


async def get_ai_execution_config(session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope) -> AIExecutionConfig:
    """Resolve one active owner workspace execution snapshot using actual settings feature flag.

    Enforces endpoint policy before returning credentials and scopes privacy
    consent to exact destinations. Explicit scope is mandatory; recheck snapshot under locks before send.
    The top-level endpoint fields mirror provider ``default``; use ``derive_for_alias`` before building a gateway.
    """
    fence = await require_settings_scope(session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled)
    row = await _row(session, scope=scope)
    if row is None:
        endpoint, credential, aliases = _defaults(settings)
        endpoint_policy_denied = False
        try:
            endpoint = _endpoint(endpoint, settings)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            endpoint, credential, endpoint_policy_denied = None, "", True
        aliases = await legacy_aliases(redis, settings, scope=scope)
        if credential:
            _cipher(settings)
        providers = {DEFAULT_PROVIDER_ID: ProviderConfig(
            kind="omniroute", label="Default", base_url=endpoint, api_key=credential, policy_denied=endpoint_policy_denied,
            credential_configured=bool(settings.omniroute_api_key.get_secret_value()),
            destination_id=_identity(endpoint, credential)[1])}
        privacy = PrivacySettings()
        revision, chat_alias, brief_alias, timeout = 1, "reasoning-large", "reasoning-small", 20
        system_prompts: dict[str, str] = {}
        web_provider, web_endpoint, web_credential, web_configured = "none", None, "", False
    else:
        providers = {pid: _resolve_provider(rec, settings) for pid, rec in _stored_providers(row).items()}
        aliases = {key: ModelMapping.model_validate(value) for key, value in row.aliases.items() if key in ALIASES}
        web_destination = f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}" if row.web_search_endpoint else None
        privacy = _privacy(row.privacy, [p.destination_id for p in providers.values() if p.destination_id and not p.local], web_destination)
        revision, chat_alias, brief_alias, timeout = row.configuration_revision, row.chat_alias, row.brief_alias, row.request_timeout_seconds
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
    default = providers.get(DEFAULT_PROVIDER_ID) or ProviderConfig(kind="omniroute", base_url=None, destination_id=None)
    # Labels are display-only and stay out of the identity; any kind/endpoint/key change rotates it.
    credential_identity = _fingerprint(json.dumps(
        {pid: (p.kind, p.base_url, _fingerprint(p.api_key), p.destination_id, p.policy_denied) for pid, p in sorted(providers.items())},
        separators=(",", ":")))
    identity = _fingerprint(json.dumps((str(scope.workspace_id), scope_actor(scope), fence.membership_revision,
        fence.configuration_revision, revision, credential_identity, {key: value.model_dump() for key, value in aliases.items()},
        privacy.model_dump(), web_provider, web_endpoint, _fingerprint(web_credential), tuple(sorted(settings.ai_allowed_endpoint_hosts)),
        tuple(sorted(settings.ai_allowed_endpoint_cidrs)), _fingerprint(settings.ai_credential_encryption_key.get_secret_value())),
        sort_keys=True, separators=(",", ":"), default=str))
    return AIExecutionConfig(
        workspace_id=scope.workspace_id, actor_user_id=scope_actor(scope), membership_revision=fence.membership_revision,
        access_configuration_revision=fence.configuration_revision,
        configuration_revision=revision, gateway_identity=identity, endpoint_destination_id=default.destination_id,
        endpoint_policy_denied=default.policy_denied, omniroute_credential_configured=default.credential_configured,
        endpoint_allowed_cidrs=tuple(settings.ai_allowed_endpoint_cidrs), allow_public_providers=settings.ai_allow_public_providers,
        omniroute_base_url=default.base_url, omniroute_api_key=default.api_key, aliases=aliases, privacy=privacy,
        chat_alias=chat_alias, brief_alias=brief_alias, request_timeout_seconds=timeout,
        web_search_provider=web_provider, web_search_endpoint=web_endpoint, web_search_api_key=web_credential,
        web_search_credential_configured=web_configured, system_prompts=system_prompts, providers=providers,
    )


def _read(config: AIExecutionConfig) -> AISettingsRead:
    """Project execution settings into the API response without secret values."""
    return AISettingsRead(
        configuration_revision=config.configuration_revision, omniroute_base_url=config.omniroute_base_url,
        endpoint_destination_id=config.endpoint_destination_id,
        omniroute_credential_configured=config.omniroute_credential_configured,
        endpoint_policy_denied=config.endpoint_policy_denied,
        providers=[ProviderRead(id=pid, kind=p.kind, label=p.label, base_url=p.base_url, destination_id=p.destination_id,
                                credential_configured=p.credential_configured, local=p.local, policy_denied=p.policy_denied)
                   for pid, p in config.providers.items()],
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        web_search_destination_id=(f"web-search:{_fingerprint(config.web_search_endpoint or '')[:32]}" if config.web_search_endpoint else None),
        web_search_credential_configured=config.web_search_credential_configured, chat_alias=config.chat_alias,
        brief_alias=config.brief_alias, aliases=config.aliases, capabilities=[], privacy=config.privacy,
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


async def save_ai_settings(session: AsyncSession, update: AISettingsUpdate, settings: Settings, *, scope: Scope,
                           auth_sessions: tuple[AccountSessionRef, ...] = ()) -> AISettingsRead:
    """Validate and persist an optimistic, revision-checked AI settings update.

    Locks admitted auth/workspace then matching owner settings, encrypts replacement credentials, and clears
    destination-bound privacy consent when its endpoint changes; the caller owns
    transaction commit/rollback.
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
    if any(alias not in ALIASES for alias in update.aliases):
        raise HTTPException(status_code=422, detail="Unknown model alias")
    bootstrap = row.configuration_revision == 1
    default_endpoint, default_credential, _ = _defaults(settings)
    try:
        default_endpoint = _endpoint(default_endpoint, settings)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        default_endpoint = None
    if bootstrap:
        row.omniroute_base_url = row.omniroute_base_url or default_endpoint
        if not row.aliases:
            row.aliases = {key: value.model_dump(mode="json") for key, value in (await legacy_aliases(None, settings, scope=scope)).items()}
        if not row.omniroute_api_key_ciphertext and default_credential:
            row.omniroute_api_key_ciphertext = _cipher(settings).encrypt(default_credential.encode()).decode()
    stored = _stored_providers(row)
    providers: dict[str, dict[str, Any]]
    if update.providers is None:
        # Legacy omniroute_* fields edit provider "default"; every other provider is untouched.
        endpoint = _endpoint(str(update.omniroute_base_url) if update.omniroute_base_url else None, settings)
        ciphertext = _kept_ciphertext(stored.get(DEFAULT_PROVIDER_ID), endpoint, settings)
        if update.omniroute_credential_action == "replaced":
            if not update.omniroute_api_key:
                raise HTTPException(status_code=422, detail="Replacement gateway credential is required")
            ciphertext = _cipher(settings).encrypt(update.omniroute_api_key.encode()).decode()
        elif update.omniroute_credential_action == "removed":
            ciphertext = None
        providers = dict(stored)
        if endpoint or ciphertext or DEFAULT_PROVIDER_ID in stored:
            providers[DEFAULT_PROVIDER_ID] = {"kind": "omniroute", "label": "Default", **stored.get(DEFAULT_PROVIDER_ID, {}),
                                              "base_url": endpoint, "key_ciphertext": ciphertext}
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
            providers[item.id] = {"kind": item.kind, "label": item.label or item.id, "base_url": endpoint,
                                  "key_ciphertext": ciphertext, "local": item.kind in LOCAL_PROVIDER_KINDS}
    for mapping in update.aliases.values():
        if mapping.provider_id is not None and mapping.provider_id not in providers:
            raise HTTPException(status_code=422, detail="Unknown model provider")
    for alias, mapping in update.aliases.items():
        if alias == "local-private" and mapping.provider_id is not None and providers[mapping.provider_id]["kind"] not in LOCAL_PROVIDER_KINDS:
            raise HTTPException(status_code=422, detail="Local-private models require a local provider")
    next_web_endpoint = _endpoint(str(update.web_search_endpoint) if update.web_search_endpoint else None, settings)
    current_web_destination = f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}" if row.web_search_endpoint else None
    next_web_destination = f"web-search:{_fingerprint(next_web_endpoint or '')[:32]}" if next_web_endpoint else None
    privacy = update.privacy.model_dump(mode="json")
    # Consent is bound to each remote provider's destination: a new provider or a changed endpoint/kind never inherits it,
    # and removed providers drop out. A legacy save (no providers) keeps the old toggle-grants-default behaviour.
    grants: dict[str, list[str]] = {"reasoning_destinations": [], "embedding_destinations": []}
    for pid, record in providers.items():
        destination = _destination(record, settings)
        unchanged = pid in stored and _destination(stored[pid], settings) == destination
        if not destination or record["kind"] in LOCAL_PROVIDER_KINDS or not unchanged:
            continue
        for enabled, key in (("allow_remote_reasoning", "reasoning_destinations"), ("allow_remote_embeddings", "embedding_destinations")):
            if privacy[enabled] and (destination in privacy[key] or (update.providers is None and pid == DEFAULT_PROVIDER_ID)):
                grants[key].append(destination)
    for enabled, key in (("allow_remote_reasoning", "reasoning_destinations"), ("allow_remote_embeddings", "embedding_destinations")):
        privacy[key] = list(dict.fromkeys(grants[key]))
        privacy[enabled] = bool(privacy[key])
    web_endpoint_changed = current_web_destination != next_web_destination
    if web_endpoint_changed or not next_web_destination or update.web_search_provider == "none":
        privacy["allow_remote_web_search"] = False
    privacy["web_search_destinations"] = [next_web_destination] if privacy["allow_remote_web_search"] else []
    row.providers = providers
    # Legacy columns mirror provider "default" for old readers.
    row.omniroute_base_url = (providers.get(DEFAULT_PROVIDER_ID) or {}).get("base_url")
    row.omniroute_api_key_ciphertext = (providers.get(DEFAULT_PROVIDER_ID) or {}).get("key_ciphertext")
    row.privacy = privacy
    row.aliases = {key: value.model_dump(mode="json") for key, value in update.aliases.items() if key in ALIASES}
    row.chat_alias, row.brief_alias = update.chat_alias, update.brief_alias
    row.web_search_provider = update.web_search_provider
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


async def admit_write(
    session: AsyncSession, kind: str, work_id: str | None = None, epoch: int | None = None,
) -> AdmissionReceipt:
    """Forward a durable write admission to Backup without exposing its persistence models."""
    from modules.backup.public import admit_write as _admit_write

    return await _admit_write(session, kind, work_id, epoch)


async def register_activity(
    session: AsyncSession, kind: str, work_id: str | None = None,
) -> ActivityReceipt:
    """Forward an admitted API/job activity registration to Backup's detached receipt contract."""
    from modules.backup.public import register_activity as _register_activity

    return await _register_activity(session, kind, work_id)


async def register_request_activity(
    request: Request, session: AsyncSession, kind: str, work_id: str | None = None,
) -> ActivityReceipt:
    """Persist one non-owner-session callback admission before it locks owner rows or commits effects."""
    from modules.backup.public import register_request_activity as _register_request_activity

    return await _register_request_activity(request, session, kind, work_id)


async def finish_activity(
    session: AsyncSession, receipt: ActivityReceipt, *, uncertain: bool = False, interrupted: bool = False,
) -> bool:
    """Forward a terminal activity receipt while retaining Backup model ownership."""
    from modules.backup.public import finish_activity as _finish_activity

    return await _finish_activity(session, receipt, uncertain=uncertain, interrupted=interrupted)


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
    session: AsyncSession, settings: Settings, redis: Redis | None, *, workspace_id: UUID, alias: str,
) -> str | None:
    """Nonsecret digest of the workspace owner's AI execution policy for one alias, or None when unusable.

    Members cannot read Settings, so the owner context is resolved internally. Covers endpoint identity,
    credential fingerprint, alias mapping version, remote/privacy flags and AI settings revision.
    """
    owner = await resolve_workspace_owner_context(
        session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled)
    if owner is None:
        return None
    config = derive_for_alias(await get_ai_execution_config(session, settings, redis, scope=owner), alias)
    mapping = config.aliases.get(alias)
    if mapping is None or config.omniroute_base_url is None or config.endpoint_policy_denied:
        return None
    destination = config.endpoint_destination_id
    if destination is None:
        return None
    # Local providers need neither a credential nor remote consent.
    if not destination.startswith("local:") and not (config.omniroute_credential_configured and config.privacy.allow_remote_reasoning):
        return None
    return hashlib.sha256(json.dumps(
        (destination, _fingerprint(config.omniroute_api_key), alias, mapping.model, mapping.version, mapping.destination,
         config.privacy.model_dump(mode="json"), config.configuration_revision),
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
