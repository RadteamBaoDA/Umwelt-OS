"""AI provider/role settings validation and the compare-and-set save."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
    DomainError,
    ValidationFailedError,
)
from core.model_gateway.client import hosted_endpoint_equivalent
from core.model_gateway.schemas import (
    DEFAULT_PROVIDER_ID,
    HOSTED_PROVIDER_ENDPOINTS,
    HOSTED_PROVIDER_KINDS,
    LOCAL_PROVIDER_KINDS,
    MAX_PROVIDERS,
    MODEL_ROLES,
    PROVIDER_KINDS_WITHOUT_MAX_TOKENS,
    AISettingsRead,
    AISettingsUpdate,
    ProviderModel,
    ProviderUpdate,
)
from core.workspaces.schemas import Scope
from modules.settings._settings_common import (
    _cipher,
    _destination,
    _endpoint,
    _fingerprint,
    _models,
    _row,
    _stored_providers,
    _web_destination,
    require_settings_scope,
    scope_actor,
)
from modules.settings._settings_execution import read_ai_settings
from modules.settings.models import (
    AISettingsRecord,
)


def _kept_ciphertext(
    previous: dict[str, Any] | None, endpoint: str | None, settings: Settings
) -> str | None:
    """Keep a stored key only while its provider endpoint is unchanged, so a saved key never reaches a new host."""
    if not previous:
        return None
    try:
        unchanged = _endpoint(previous.get("base_url"), settings) == endpoint
    except (DomainError, HTTPException):
        unchanged = False
    return previous.get("key_ciphertext") if unchanged else None


ROLE_MODEL_TYPE_LABEL = {
    "chat": "llm",
    "brief": "llm",
    "background": "background or llm",
    "embedding": "embedding",
    "reranker": "reranker",
    "vision": "vision-capable llm",
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


def _provider_endpoint(
    item: ProviderUpdate, previous: dict[str, Any] | None
) -> tuple[str, str | None]:
    """Choose the ``(kind_to_store, raw_url)`` for one provider being saved, before ``_endpoint`` policy validation.

    Self-hosted kinds keep the client URL. Hosted kinds ignore it: a previous URL equivalent to the vendor endpoint is
    kept verbatim (rewriting it would rotate ``destination_id``, dropping the key and privacy consent); anything
    else gets the fixed endpoint. Legacy rows are shown to the UI as ``custom`` (read-time migration), so a client
    sending a hosted kind is a deliberate switch and must never bind a new key to the legacy proxy host.
    """
    if item.kind not in HOSTED_PROVIDER_KINDS:
        return item.kind, str(item.base_url) if item.base_url else None
    prev_url = previous.get("base_url") if previous else None
    if prev_url and hosted_endpoint_equivalent(item.kind, prev_url):
        return item.kind, prev_url
    return item.kind, HOSTED_PROVIDER_ENDPOINTS[item.kind]


def _build_provider_records(
    update: AISettingsUpdate,
    stored: dict[str, dict[str, Any]],
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    """Validate the submitted provider list and build the records to store (pure, no I/O).

    Without a list the stored providers are kept (they are edited only as a full list). Otherwise
    enforces the provider-count bound, unique provider and model ids and a resolvable endpoint, keeps
    the stored ciphertext only for an unchanged endpoint and encrypts replacement credentials.
    Raises ``ValidationFailedError`` (422).
    """
    if update.providers is None:
        return dict(stored)  # providers are edited only by a full list
    if len(update.providers) > MAX_PROVIDERS:
        raise ValidationFailedError(detail="Too many providers")
    if len({item.id for item in update.providers}) != len(update.providers):
        raise ValidationFailedError(detail="Duplicate provider id")
    providers: dict[str, dict[str, Any]] = {}
    for item in update.providers:
        kind_to_store, raw_url = _provider_endpoint(item, stored.get(item.id))
        endpoint = _endpoint(raw_url, settings)
        if endpoint is None:
            raise ValidationFailedError(detail="Provider endpoint is required")
        ciphertext = _kept_ciphertext(stored.get(item.id), endpoint, settings)
        if item.credential_action == "replaced":
            if not item.api_key:
                raise ValidationFailedError(detail="Replacement provider credential is required")
            ciphertext = _cipher(settings).encrypt(item.api_key.encode()).decode()
        elif item.credential_action == "removed":
            ciphertext = None
        if len({m.id for m in item.models}) != len(item.models):
            raise ValidationFailedError(detail="Duplicate model id")
        providers[item.id] = {
            "kind": kind_to_store,
            "label": item.label or item.id,
            "base_url": endpoint,
            "key_ciphertext": ciphertext,
            "local": kind_to_store in LOCAL_PROVIDER_KINDS,
            "models": [m.model_dump(mode="json") for m in item.models],
        }
    return providers


def _validated_role_mapping(role: str, mapping: Any, providers: dict[str, dict[str, Any]]) -> Any:
    """Validate one role's chosen model mapping against the provider list; return the mapping to store.

    The model must come from the provider's discovered list and match the role's model type;
    dimensions apply only to the embedding role (which requires them), reasoning effort only to chat
    and max output tokens only to LLM roles. Hosted clouds that pick their own limit get a
    client-sent ``max_output_tokens`` discarded (the server is authoritative). Raises 422.
    """
    provider = providers.get(mapping.provider_id or DEFAULT_PROVIDER_ID) or (
        next(iter(providers.values()), None) if mapping.provider_id is None else None
    )
    # A chosen model must come from the provider's discovered list and match the role's model type.
    listed = {m.id: m for m in _models(provider)} if provider else {}
    if mapping.model and not listed:
        raise ValidationFailedError(detail="Discover models for this provider first")
    if mapping.model and mapping.model not in listed:
        raise ValidationFailedError(
            detail=f"Model {mapping.model} is not in the provider's model list"
        )
    chosen = listed.get(mapping.model) if mapping.model else None
    if chosen is not None and not role_accepts(role, chosen):
        raise ValidationFailedError(
            detail=f"Role {role} needs a {ROLE_MODEL_TYPE_LABEL[role]} model"
        )
    if mapping.dimensions is not None and role != "embedding":
        raise ValidationFailedError(detail="Dimensions apply only to the embedding role")
    if mapping.reasoning_effort != "off" and role != "chat":
        raise ValidationFailedError(detail="Reasoning effort applies only to the chat role")
    if mapping.max_output_tokens is not None:
        if role not in {"chat", "brief", "background", "vision"}:
            raise ValidationFailedError(detail="Max output tokens apply only to LLM roles")
        if provider is not None and provider.get("kind") in PROVIDER_KINDS_WITHOUT_MAX_TOKENS:
            # Server is authoritative: hosted clouds pick their own limit, so a client-sent value is discarded.
            mapping = mapping.model_copy(update={"max_output_tokens": None})
    if role == "embedding" and mapping.model and mapping.dimensions is None:
        raise ValidationFailedError(
            detail="Embedding dimensions required; run the embeddings probe"
        )
    return mapping


def _chosen_defaults(
    update: AISettingsUpdate, providers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Collect and validate the roles the update maps to models (all provider ids checked first)."""
    chosen_defaults = {
        role: mapping
        for role in MODEL_ROLES
        if (mapping := getattr(update.defaults, role)) is not None
    }
    for mapping in chosen_defaults.values():
        if mapping.provider_id is not None and mapping.provider_id not in providers:
            raise ValidationFailedError(detail="Unknown model provider")
    return {
        role: _validated_role_mapping(role, mapping, providers)
        for role, mapping in chosen_defaults.items()
    }


def _consented_privacy(
    update: AISettingsUpdate,
    row: AISettingsRecord,
    providers: dict[str, dict[str, Any]],
    stored: dict[str, dict[str, Any]],
    settings: Settings,
    current_web_destination: str | None,
    next_web_destination: str | None,
) -> dict[str, Any]:
    """Return the privacy block with every consent re-bound to the destinations that survive this save.

    Consent is bound to each remote provider's destination: a new provider or a changed endpoint/kind
    never inherits it, and removed providers drop out. Existing web-search fallback grants survive model
    saves but cannot be created through this legacy API; the primary web-search grant survives only when
    its destination is unchanged. A flag with no surviving destination is switched off. Pure.
    """
    privacy = update.privacy.model_dump(mode="json")
    grants: dict[str, list[str]] = {"reasoning_destinations": [], "embedding_destinations": []}
    for pid, record in providers.items():
        destination = _destination(record, settings)
        unchanged = pid in stored and _destination(stored[pid], settings) == destination
        if not destination or record["kind"] in LOCAL_PROVIDER_KINDS or not unchanged:
            continue
        for enabled, key in (
            ("allow_remote_reasoning", "reasoning_destinations"),
            ("allow_remote_embeddings", "embedding_destinations"),
        ):
            if privacy[enabled] and destination in privacy[key]:
                grants[key].append(destination)
    for enabled, key in (
        ("allow_remote_reasoning", "reasoning_destinations"),
        ("allow_remote_embeddings", "embedding_destinations"),
    ):
        privacy[key] = list(dict.fromkeys(grants[key]))
        privacy[enabled] = bool(privacy[key])
    # Existing fallback grants survive model saves but cannot be created through this legacy API.
    prior_web = (row.privacy or {}).get("web_search_destinations", [])
    prior_web = prior_web if isinstance(prior_web, list) else []
    fallback_destinations = [
        _web_destination(item.get("endpoint")) for item in (row.web_search_fallbacks or [])
    ]
    web_grants = [
        d
        for d in fallback_destinations
        if d and d in prior_web and d in privacy["web_search_destinations"]
    ]
    if (
        privacy["allow_remote_web_search"]
        and current_web_destination == next_web_destination
        and next_web_destination
        and update.web_search_provider != "none"
    ):
        web_grants.insert(0, next_web_destination)
    privacy["web_search_destinations"] = (
        list(dict.fromkeys(web_grants)) if privacy["allow_remote_web_search"] else []
    )
    privacy["allow_remote_web_search"] = bool(privacy["web_search_destinations"])
    return privacy


async def save_ai_settings(
    session: AsyncSession,
    update: AISettingsUpdate,
    settings: Settings,
    *,
    scope: Scope,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> AISettingsRead:
    """Validate and persist an optimistic, revision-checked AI settings update.

    Locks admitted auth/workspace then matching owner settings, encrypts replacement credentials, and clears
    destination-bound privacy consent when its endpoint changes; the caller owns
    transaction commit/rollback. Chosen models must be discovered on their provider and match the role's model type
    (see ``role_accepts``); the embedding role must carry the probed vector dimensions and no other role may.
    """
    await require_settings_scope(
        session,
        scope=scope,
        multi_workspace_enabled=settings.multi_workspace_enabled,
        locked=True,
        auth_sessions=auth_sessions,
    )
    # Composite schema ownership and scoped reread prevent borrowing another owner's PK.
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
    stored = _stored_providers(row)
    providers = _build_provider_records(update, stored, settings)
    chosen_defaults = _chosen_defaults(update, providers)
    next_web_endpoint = _endpoint(
        str(update.web_search_endpoint) if update.web_search_endpoint else None, settings
    )
    if (
        update.web_search_provider != "none"
        and next_web_endpoint
        and any(
            item.get("endpoint") == next_web_endpoint for item in (row.web_search_fallbacks or [])
        )
    ):
        raise ValidationFailedError(detail="Web search endpoints must be unique")
    current_web_destination = (
        f"web-search:{_fingerprint(row.web_search_endpoint or '')[:32]}"
        if row.web_search_endpoint
        else None
    )
    next_web_destination = (
        f"web-search:{_fingerprint(next_web_endpoint or '')[:32]}" if next_web_endpoint else None
    )
    privacy = _consented_privacy(
        update,
        row,
        providers,
        stored,
        settings,
        current_web_destination,
        next_web_destination,
    )
    row.providers = providers
    row.privacy = privacy
    row.defaults = {
        role: mapping.model_dump(mode="json") for role, mapping in chosen_defaults.items()
    }
    row.web_search_provider = update.web_search_provider
    if current_web_destination != next_web_destination:
        row.web_search_api_key_ciphertext = None  # Never reuse a key at a changed destination.
    row.web_search_endpoint = next_web_endpoint
    if update.web_search_credential_action == "replaced":
        if not update.web_search_api_key:
            raise ValidationFailedError(detail="Replacement search credential is required")
        row.web_search_api_key_ciphertext = (
            _cipher(settings).encrypt(update.web_search_api_key.encode()).decode()
        )
    elif update.web_search_credential_action == "removed":
        row.web_search_api_key_ciphertext = None
    row.request_timeout_seconds = update.request_timeout_seconds
    row.configuration_revision += 1
    await session.flush()
    return await read_ai_settings(session, settings, scope=scope)
