from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from core.config import Settings
from core.model_gateway.schemas import (
    AIExecutionConfig,
    AISettingsRead,
    AISettingsUpdate,
    ModelMapping,
    PrivacySettings,
)
from modules.backup.schemas import ActivityReceipt, AdmissionReceipt
from modules.settings.models import AISettingsRecord, OwnerPreferencesRecord, legacy_aliases
from modules.settings.schemas import (
    ModuleLifecycleRead,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
)

OWNER_ID = 1
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
    try:
        parsed_address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        normalized_host = parts.hostname.encode("idna").decode("ascii").lower()
        is_ipv6 = False
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
    if (policy_authority not in settings.ai_allowed_endpoint_hosts
            and effective_authority not in settings.ai_allowed_endpoint_hosts):
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


async def _row(session: AsyncSession) -> AISettingsRecord | None:
    """Read the owner singleton while refreshing any identity-mapped row."""
    return await session.scalar(select(AISettingsRecord).where(AISettingsRecord.owner_id == OWNER_ID).execution_options(populate_existing=True))


def _defaults(settings: Settings) -> tuple[str | None, str, dict[str, ModelMapping]]:
    """Read deployment-provided endpoint, key, and supported model defaults."""
    endpoint = str(settings.omniroute_base_url) if settings.omniroute_base_url else None
    aliases = {name: ModelMapping(model=model, destination="remote") for name, model in settings.omniroute_models.items() if name in ALIASES}
    return endpoint, settings.omniroute_api_key.get_secret_value(), aliases


def _privacy(raw: dict[str, object] | None, destination: str | None, web_destination: str | None) -> PrivacySettings:
    """Expose only explicit privacy grants bound to the current destinations."""
    raw = raw or {}
    def granted(key: str, enabled: str) -> bool:
        """Check that a grant is enabled and includes this exact destination."""
        values = raw.get(key)
        return bool(destination and raw.get(enabled) and isinstance(values, list) and destination in values)

    reasoning = granted("reasoning_destinations", "allow_remote_reasoning")
    embeddings = granted("embedding_destinations", "allow_remote_embeddings")
    web_values = raw.get("web_search_destinations")
    web_search = bool(web_destination and raw.get("allow_remote_web_search")
                      and isinstance(web_values, list) and web_destination in web_values)
    return PrivacySettings(
        allow_remote_reasoning=reasoning, allow_remote_embeddings=embeddings,
        allow_remote_web_search=web_search,
        reasoning_destinations=[destination] if reasoning else [],
        embedding_destinations=[destination] if embeddings else [],
        web_search_destinations=[web_destination] if web_search and web_destination else [],
    )


def _identity(endpoint: str | None, credential: str) -> tuple[str, str | None]:
    """Derive non-secret gateway and destination identifiers from configuration."""
    destination = f"omniroute:{_fingerprint(endpoint or '')[:32]}" if endpoint else None
    return _fingerprint(json.dumps((endpoint, _fingerprint(credential)), separators=(",", ":"))), destination


async def get_ai_execution_config(session: AsyncSession, settings: Settings, redis: Redis | None = None) -> AIExecutionConfig:
    """Resolve effective AI settings from persisted values and deployment defaults.

    Enforces endpoint policy before returning credentials and scopes privacy
    consent to the currently selected destinations.
    """
    row = await _row(session)
    if row is None:
        endpoint, credential, aliases = _defaults(settings)
        endpoint_policy_denied = False
        try:
            endpoint = _endpoint(endpoint, settings)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            endpoint, credential, endpoint_policy_denied = None, "", True
        aliases = await legacy_aliases(redis, settings)
        if credential:
            _cipher(settings)
        credential_configured = bool(settings.omniroute_api_key.get_secret_value())
    if row is not None:
        endpoint_policy_denied = False
        try:
            endpoint = _endpoint(row.omniroute_base_url, settings)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            endpoint, endpoint_policy_denied = None, True
        credential_configured = bool(row.omniroute_api_key_ciphertext)
        credential = "" if endpoint_policy_denied else _decrypt(row.omniroute_api_key_ciphertext, settings)
        aliases = {key: ModelMapping.model_validate(value) for key, value in row.aliases.items() if key in ALIASES}
        web_destination = f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}" if row.web_search_endpoint else None
        privacy = _privacy(
            row.privacy,
            None if endpoint_policy_denied else _identity(endpoint, credential)[1],
            web_destination,
        )
        revision, chat_alias, brief_alias, timeout = row.configuration_revision, row.chat_alias, row.brief_alias, row.request_timeout_seconds
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
    else:
        privacy = PrivacySettings()
        revision, chat_alias, brief_alias, timeout = 1, "reasoning-large", "reasoning-small", 20
        web_provider, web_endpoint, web_credential, web_configured = "none", None, "", False
    identity, destination = _identity(endpoint, credential)
    return AIExecutionConfig(
        configuration_revision=revision, gateway_identity=identity, endpoint_destination_id=destination,
        endpoint_policy_denied=endpoint_policy_denied, omniroute_credential_configured=credential_configured,
        endpoint_allowed_cidrs=tuple(settings.ai_allowed_endpoint_cidrs),
        omniroute_base_url=endpoint, omniroute_api_key=credential, aliases=aliases, privacy=privacy,
        chat_alias=chat_alias, brief_alias=brief_alias, request_timeout_seconds=timeout,
        web_search_provider=web_provider, web_search_endpoint=web_endpoint, web_search_api_key=web_credential,
        web_search_credential_configured=web_configured,
    )


def _read(config: AIExecutionConfig) -> AISettingsRead:
    """Project execution settings into the API response without secret values."""
    return AISettingsRead(
        configuration_revision=config.configuration_revision, omniroute_base_url=config.omniroute_base_url,
        endpoint_destination_id=config.endpoint_destination_id,
        omniroute_credential_configured=config.omniroute_credential_configured,
        endpoint_policy_denied=config.endpoint_policy_denied,
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        web_search_destination_id=(f"web-search:{_fingerprint(config.web_search_endpoint or '')[:32]}" if config.web_search_endpoint else None),
        web_search_credential_configured=config.web_search_credential_configured, chat_alias=config.chat_alias,
        brief_alias=config.brief_alias, aliases=config.aliases, capabilities=[], privacy=config.privacy,
        request_timeout_seconds=config.request_timeout_seconds,
    )


async def lock_ai_settings_for_share(session: AsyncSession) -> bool:
    """Share-lock the owner AI settings row until the caller's transaction ends.

    Blocks ``save_ai_settings`` (row ``FOR UPDATE``) so a consent/provider/endpoint/key read in the same
    transaction cannot be revoked before the caller releases it. A leaf lock: savers lock nothing else.
    Returns False when no row exists (nothing was locked; callers treat that as "not configured").
    """
    return (await session.scalar(
        select(AISettingsRecord.owner_id).where(AISettingsRecord.owner_id == OWNER_ID).with_for_update(read=True)
    )) is not None


async def read_ai_settings(session: AsyncSession, settings: Settings, redis: Redis | None = None) -> AISettingsRead:
    """Return the public AI settings projection for the current owner."""
    return _read(await get_ai_execution_config(session, settings, redis))


async def save_ai_settings(session: AsyncSession, update: AISettingsUpdate, settings: Settings) -> AISettingsRead:
    """Validate and persist an optimistic, revision-checked AI settings update.

    Locks the owner singleton, encrypts replacement credentials, and clears
    destination-bound privacy consent when its endpoint changes; the caller owns
    transaction commit/rollback.
    """
    # Create then lock the owner singleton so concurrent first saves share the same CAS boundary.
    await session.execute(insert(AISettingsRecord).values(owner_id=OWNER_ID).on_conflict_do_nothing(index_elements=["owner_id"]))
    row = await session.scalar(select(AISettingsRecord).where(AISettingsRecord.owner_id == OWNER_ID).with_for_update())
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise HTTPException(status_code=409, detail="AI settings changed; reload before saving")
    if any(alias not in ALIASES for alias in update.aliases):
        raise HTTPException(status_code=422, detail="Unknown model alias")
    endpoint = _endpoint(str(update.omniroute_base_url) if update.omniroute_base_url else None, settings)
    old_endpoint = row.omniroute_base_url
    bootstrap = row.configuration_revision == 1
    default_endpoint, default_credential, _ = _defaults(settings)
    try:
        default_endpoint = _endpoint(default_endpoint, settings)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        default_endpoint = None
    if bootstrap:
        old_endpoint = old_endpoint or default_endpoint
        row.omniroute_base_url = old_endpoint
        if not row.aliases:
            row.aliases = {key: value.model_dump() for key, value in (await legacy_aliases(None, settings)).items()}
        if not row.omniroute_api_key_ciphertext and default_credential:
            row.omniroute_api_key_ciphertext = _cipher(settings).encrypt(default_credential.encode()).decode()
    if update.omniroute_credential_action == "replaced":
        if not update.omniroute_api_key:
            raise HTTPException(status_code=422, detail="Replacement gateway credential is required")
        row.omniroute_api_key_ciphertext = _cipher(settings).encrypt(update.omniroute_api_key.encode()).decode()
    elif update.omniroute_credential_action == "removed":
        row.omniroute_api_key_ciphertext = None
    row.omniroute_base_url = endpoint
    credential = _decrypt(row.omniroute_api_key_ciphertext, settings) if row.omniroute_api_key_ciphertext else ""
    _, destination = _identity(endpoint, credential)
    next_web_endpoint = _endpoint(str(update.web_search_endpoint) if update.web_search_endpoint else None, settings)
    current_web_destination = f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}" if row.web_search_endpoint else None
    next_web_destination = f"web-search:{_fingerprint(next_web_endpoint or '')[:32]}" if next_web_endpoint else None
    privacy = update.privacy.model_dump()
    # Consent is bound to the endpoint selected in this same owner save.
    try:
        canonical_old_endpoint = _endpoint(old_endpoint, settings)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        canonical_old_endpoint = None
    endpoint_changed = canonical_old_endpoint != endpoint
    web_endpoint_changed = current_web_destination != next_web_destination
    for enabled, key in (("allow_remote_reasoning", "reasoning_destinations"), ("allow_remote_embeddings", "embedding_destinations")):
        if endpoint_changed or not destination:
            privacy[enabled] = False
        privacy[key] = [destination] if privacy[enabled] and destination else []
    if web_endpoint_changed or not next_web_destination or update.web_search_provider == "none":
        privacy["allow_remote_web_search"] = False
    privacy["web_search_destinations"] = [next_web_destination] if privacy["allow_remote_web_search"] else []
    row.privacy = privacy
    row.aliases = {key: value.model_dump() for key, value in update.aliases.items() if key in ALIASES}
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
    return await read_ai_settings(session, settings)


async def read_owner_preferences(session: AsyncSession) -> OwnerPreferencesRead:
    """Return persisted preferences or the documented defaults for a new owner."""
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == OWNER_ID)
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


async def read_module_availability(session: AsyncSession) -> ModuleLifecycleRead:
    """Return the persisted effective module view for cross-module owner boundaries."""
    from modules.settings.lifecycle import read_module_lifecycle

    return await read_module_lifecycle(session)


async def read_retention_settings(session: AsyncSession) -> RetentionSettingsRead:
    """Return the owner-approved retention policy through Settings' public projection."""
    from modules.settings.lifecycle import read_retention_settings as _read_retention_settings

    return await _read_retention_settings(session)


async def module_is_enabled(session: AsyncSession, module_id: str) -> bool:
    """Resolve current dependency-derived module availability for worker and tool dispatch."""
    from modules.settings.lifecycle import module_is_enabled as _module_is_enabled

    return await _module_is_enabled(session, module_id)


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
    """Build the owner-first request dependency for routes owned by another module."""
    from modules.settings.lifecycle import module_dependency as _module_dependency

    return _module_dependency(module_id)


async def save_owner_preferences(
    session: AsyncSession,
    update: OwnerPreferencesUpdate,
) -> OwnerPreferencesRead:
    """Persist an optimistic, revision-checked owner preference update.

    Creates then locks the singleton row to serialize first writes; transaction
    completion remains the caller's responsibility.
    """
    await session.execute(
        insert(OwnerPreferencesRecord)
        .values(owner_id=OWNER_ID)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == OWNER_ID)
        .with_for_update()
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
