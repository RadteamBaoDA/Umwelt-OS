"""AI execution-config snapshot, fences and system-prompt persistence."""

from __future__ import annotations

import json

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
    DomainError,
    ValidationFailedError,
)
from core.model_gateway.schemas import (
    DEFAULT_PROVIDER_ID,
    MODEL_ROLES,
    AIExecutionConfig,
    AISettingsRead,
    ModelDefaults,
    PrivacySettings,
    ProviderConfig,
    ProviderRead,
    WebSearchTarget,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.settings._settings.common import (
    _decrypt,
    _fingerprint,
    _privacy,
    _resolve_provider,
    _row,
    _stored_providers,
    _web_destination,
    require_settings_scope,
    scope_actor,
)
from modules.settings.models import (
    AISettingsRecord,
)
from modules.settings.prompts import (
    DEFAULT_SYSTEM_PROMPTS,
    MAX_PROMPT_CHARS,
)
from modules.settings.schemas import (
    SystemPromptEntry,
    SystemPromptsRead,
    SystemPromptsUpdate,
)


async def get_ai_execution_config(
    session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope
) -> AIExecutionConfig:
    """Resolve one active owner workspace execution snapshot using actual settings feature flag.

    Enforces endpoint policy before returning credentials and scopes privacy
    consent to exact destinations. Explicit scope is mandatory; recheck snapshot under locks before send.
    The top-level endpoint fields mirror provider ``default``; use ``derive_for_role`` before building a gateway.
    """
    fence = await require_settings_scope(
        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled
    )
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
        providers = {
            pid: _resolve_provider(rec, settings) for pid, rec in _stored_providers(row).items()
        }
        defaults = ModelDefaults.model_validate(
            {
                key: value
                for key, value in (row.defaults or {}).items()
                if key in MODEL_ROLES and value
            }
        )
        fallback_targets = []
        for item in row.web_search_fallbacks or []:
            try:
                fallback_key = _decrypt(item.get("key_ciphertext"), settings)
            except (DomainError, HTTPException) as exc:
                if exc.status_code != 503:
                    raise
                fallback_key = ""  # A bad fallback secret must not disable unrelated capabilities.
            fallback_targets.append(
                WebSearchTarget(
                    provider=item["provider"],
                    endpoint=item["endpoint"],
                    api_key=fallback_key,
                    credential_configured=bool(item.get("key_ciphertext")),
                )
            )
        web_fallbacks = tuple(fallback_targets)
        web_destinations = [
            d
            for endpoint in (row.web_search_endpoint, *(t.endpoint for t in web_fallbacks))
            if (d := _web_destination(endpoint))
        ]
        privacy = _privacy(
            row.privacy,
            [p.destination_id for p in providers.values() if p.destination_id and not p.local],
            web_destinations,
        )
        revision, timeout = row.configuration_revision, row.request_timeout_seconds
        system_prompts = dict(row.system_prompts or {})
        web_provider, web_endpoint = row.web_search_provider, row.web_search_endpoint
        web_configured = bool(row.web_search_api_key_ciphertext)
        try:
            web_credential = _decrypt(row.web_search_api_key_ciphertext, settings)
        except (DomainError, HTTPException) as exc:
            if exc.status_code != 503:
                raise
            # Web search is an independent capability; a broken web-search key
            # must not block lexical or semantic retrieval.
            web_credential = ""
    default = (
        providers.get(DEFAULT_PROVIDER_ID)
        or next(iter(providers.values()), None)
        or ProviderConfig(kind="custom", base_url=None, destination_id=None)
    )
    # Labels are display-only and stay out of the identity; any kind/endpoint/key change rotates it.
    credential_identity = _fingerprint(
        json.dumps(
            {
                pid: (
                    p.kind,
                    p.base_url,
                    _fingerprint(p.api_key),
                    p.destination_id,
                    p.policy_denied,
                )
                for pid, p in sorted(providers.items())
            },
            separators=(",", ":"),
        )
    )
    identity = _fingerprint(
        json.dumps(
            (
                str(scope.workspace_id),
                scope_actor(scope),
                fence.membership_revision,
                fence.configuration_revision,
                revision,
                credential_identity,
                defaults.model_dump(),
                privacy.model_dump(),
                web_provider,
                web_endpoint,
                _fingerprint(web_credential),
                [(t.provider, t.endpoint, _fingerprint(t.api_key)) for t in web_fallbacks],
                tuple(sorted(settings.ai_allowed_endpoint_hosts)),
                tuple(sorted(settings.ai_allowed_endpoint_cidrs)),
                _fingerprint(settings.ai_credential_encryption_key.get_secret_value()),
            ),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    # Model evidence/embedding validity identity: only what decides whether a model answers or an embedding
    # space matches. Deliberately excludes the ai_settings row revision, web search config/consent, system
    # prompts and owner prefs, so saving those never discards verified capabilities or search generations.
    model_identity = _fingerprint(
        json.dumps(
            (
                str(scope.workspace_id),
                scope_actor(scope),
                fence.membership_revision,
                fence.configuration_revision,
                credential_identity,
                defaults.model_dump(),
                privacy.model_dump(
                    include={
                        "allow_remote_reasoning",
                        "allow_remote_embeddings",
                        "reasoning_destinations",
                        "embedding_destinations",
                    }
                ),
                tuple(sorted(settings.ai_allowed_endpoint_hosts)),
                tuple(sorted(settings.ai_allowed_endpoint_cidrs)),
                _fingerprint(settings.ai_credential_encryption_key.get_secret_value()),
            ),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    return AIExecutionConfig(
        workspace_id=scope.workspace_id,
        actor_user_id=scope_actor(scope),
        membership_revision=fence.membership_revision,
        access_configuration_revision=fence.configuration_revision,
        configuration_revision=revision,
        gateway_identity=identity,
        model_identity=model_identity,
        endpoint_destination_id=default.destination_id,
        endpoint_policy_denied=default.policy_denied,
        gateway_credential_configured=default.credential_configured,
        endpoint_allowed_cidrs=tuple(settings.ai_allowed_endpoint_cidrs),
        allow_public_providers=settings.ai_allow_public_providers,
        gateway_base_url=default.base_url,
        gateway_api_key=default.api_key,
        gateway_kind=default.kind,
        defaults=defaults,
        privacy=privacy,
        request_timeout_seconds=timeout,
        web_search_provider=web_provider,
        web_search_endpoint=web_endpoint,
        web_search_api_key=web_credential,
        web_search_credential_configured=web_configured,
        web_search_fallbacks=web_fallbacks,
        system_prompts=system_prompts,
        providers=providers,
    )


def _read(config: AIExecutionConfig) -> AISettingsRead:
    """Project execution settings into the API response without secret values."""
    return AISettingsRead(
        configuration_revision=config.configuration_revision,
        endpoint_destination_id=config.endpoint_destination_id,
        endpoint_policy_denied=config.endpoint_policy_denied,
        providers=[
            ProviderRead(
                id=pid,
                kind=p.kind,
                label=p.label,
                base_url=p.base_url,
                destination_id=p.destination_id,
                credential_configured=p.credential_configured,
                local=p.local,
                policy_denied=p.policy_denied,
                models=list(p.models),
            )
            for pid, p in config.providers.items()
        ],
        web_search_provider=config.web_search_provider,
        web_search_endpoint=config.web_search_endpoint,
        web_search_destination_id=(
            f"web-search:{_fingerprint(config.web_search_endpoint or '')[:32]}"
            if config.web_search_endpoint
            else None
        ),
        web_search_credential_configured=config.web_search_credential_configured,
        defaults=config.defaults,
        capabilities=[],
        privacy=config.privacy,
        request_timeout_seconds=config.request_timeout_seconds,
    )


async def check_ai_execution_config(
    session: AsyncSession,
    settings: Settings,
    redis: Redis | None,
    *,
    scope: Scope,
    expected: AIExecutionConfig,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> None:
    """Lock current identity/settings and reject a changed execution snapshot before send/publish.

    Exact HTTP session refs are mandatory at HTTP callers. Workers supply their admitted
    durable scope and separately validate resource/claim/provenance. Caller must rollback
    or close this short transaction before network, then reacquire for publication.
    No transport, commit or ambient scope occurs here.
    """
    fence = AccessFence(
        workspace_id=expected.workspace_id,
        user_id=expected.actor_user_id,
        membership_revision=expected.membership_revision,
        configuration_revision=expected.access_configuration_revision,
    )
    await require_settings_scope(
        session,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        locked=True,
        expected=fence,
        auth_sessions=auth_sessions,
    )
    await _row(session, scope=scope, locked=True)
    latest = await get_ai_execution_config(session, settings, redis, scope=scope)
    if (
        latest.gateway_identity != expected.gateway_identity
        or latest.configuration_revision != expected.configuration_revision
    ):
        raise ConflictError(detail="AI settings changed; prepare a fresh request")


async def lock_ai_settings_for_share(session: AsyncSession, *, scope: Scope) -> bool:
    """Share-lock the scoped AI settings row until the caller's transaction ends.

    Blocks ``save_ai_settings`` (row ``FOR UPDATE``) so a consent/provider/endpoint/key read in the same
    transaction cannot be revoked before the caller releases it. A leaf lock: savers lock nothing else.
    The caller already holds the access fence. Returns False when no row exists.
    """
    return (
        await session.scalar(
            select(AISettingsRecord.owner_id)
            .where(
                AISettingsRecord.owner_id == scope_actor(scope),
                AISettingsRecord.workspace_id == scope.workspace_id,
            )
            .with_for_update(read=True)
        )
    ) is not None


async def read_ai_settings(
    session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope
) -> AISettingsRead:
    """Return owner-only explicit workspace settings projection without plaintext secrets."""
    return _read(await get_ai_execution_config(session, settings, redis, scope=scope))


def _prompts_read(revision: int, overrides: dict[str, str]) -> SystemPromptsRead:
    """Project overrides over code defaults for the prompts page."""
    return SystemPromptsRead(
        configuration_revision=revision,
        prompts=[
            SystemPromptEntry(
                key=key,
                value=overrides.get(key) or default,
                default=default,
                customized=key in overrides,
            )
            for key, default in DEFAULT_SYSTEM_PROMPTS.items()
        ],
    )


async def read_system_prompts(
    session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope
) -> SystemPromptsRead:
    """Return effective system prompts for the owner workspace."""
    config = await get_ai_execution_config(session, settings, redis, scope=scope)
    return _prompts_read(config.configuration_revision, config.system_prompts)


async def save_system_prompts(
    session: AsyncSession,
    update: SystemPromptsUpdate,
    settings: Settings,
    *,
    scope: Scope,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> SystemPromptsRead:
    """Revision-checked merge of prompt overrides; blank/None/default text resets a key.

    Bumps ``configuration_revision`` so in-flight runs holding an older snapshot fail their pre-send check.
    """
    if any(key not in DEFAULT_SYSTEM_PROMPTS for key in update.prompts):
        raise ValidationFailedError(detail="Unknown system prompt")
    if any(value and len(value) > MAX_PROMPT_CHARS for value in update.prompts.values()):
        raise ValidationFailedError(detail="System prompt too long")
    await require_settings_scope(
        session,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        locked=True,
        auth_sessions=auth_sessions,
    )
    await session.execute(
        insert(AISettingsRecord)
        .values(owner_id=scope_actor(scope), workspace_id=scope.workspace_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await _row(session, scope=scope, locked=True)
    if row is None:
        raise RuntimeError("AI settings singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise ConflictError(detail="AI settings changed; reload before saving")
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
