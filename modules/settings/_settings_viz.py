"""Visualization router settings: resolution, gateway config, read and save."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field
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
    LOCAL_PROVIDER_KINDS,
    AIExecutionConfig,
    ModelMapping,
)
from core.workspaces.schemas import Scope
from modules.settings._settings_common import (
    VIZ_CLOUDFLARE_ACCOUNT,
    VIZ_CLOUDFLARE_URL,
    VIZ_KEYED_KINDS,
    VIZ_OPENROUTER_URL,
    VIZ_TYPESAFE_DESTINATION,
    _cipher,
    _decrypt,
    _destination,
    _endpoint,
    _fingerprint,
    _identity,
    _models,
    _row,
    _stored_providers,
    derive_for_mapping,
    require_settings_scope,
    scope_actor,
)
from modules.settings.models import (
    AISettingsRecord,
)
from modules.settings.schemas import (
    VizRouterRead,
    VizRouterUpdate,
)


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
            VIZ_CLOUDFLARE_URL.format(account_id=account)
            if VIZ_CLOUDFLARE_ACCOUNT.fullmatch(account)
            else None
        )
        if url is None or not url.lower().startswith("https://"):
            raise ValidationFailedError(
                detail="Cloudflare needs a 32-character account id or an https gateway URL"
            )
    else:
        return None
    return _endpoint(url, settings)


def _viz_destination(
    stored: dict[str, Any], providers: dict[str, dict[str, Any]], settings: Settings
) -> str | None:
    """Destination id the stored router would send to: TypeSafe host, own endpoint (openrouter/cloudflare) or the provider's.

    Own-endpoint kinds derive the id from the validated URL, so changing the Cloudflare account or gateway changes it
    and drops saved consent; a policy-denied endpoint has no destination (None).
    """
    if stored.get("kind") == "typesafe":
        return VIZ_TYPESAFE_DESTINATION
    if stored.get("kind") in VIZ_KEYED_KINDS:
        try:
            endpoint = _viz_endpoint(stored, settings)
        except (DomainError, HTTPException) as exc:
            if exc.status_code != 422:
                raise
            return None
        return _identity(endpoint, "")[1]
    if stored.get("kind") == "llm":
        record = providers.get(stored.get("provider_id") or DEFAULT_PROVIDER_ID)
        return _destination(record, settings) if record else None
    return None


async def resolve_viz_router(
    session: AsyncSession, settings: Settings, *, scope: Scope
) -> VizRouterResolved:
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
    record = (
        providers.get(stored.get("provider_id") or DEFAULT_PROVIDER_ID) if kind == "llm" else None
    )
    local = bool(record and record.get("kind") in LOCAL_PROVIDER_KINDS)
    consent = (
        bool(stored.get("allow_remote_visualization_routing"))
        and destination is not None
        and stored.get("consent_destination") == destination
    )
    key = ""
    ciphertext = stored.get(f"{kind}_key_ciphertext")
    if kind in {"typesafe", *VIZ_KEYED_KINDS} and ciphertext:
        try:
            key = _decrypt(ciphertext, settings)
        except (DomainError, HTTPException):
            key = ""
    base_url = None
    if kind in VIZ_KEYED_KINDS:
        try:
            base_url = _viz_endpoint(stored, settings)
        except (DomainError, HTTPException) as exc:
            if exc.status_code != 422:
                raise
    return VizRouterResolved(
        kind=kind,
        provider_id=stored.get("provider_id"),
        model=stored.get("model", ""),
        mode=stored.get("mode", "json_schema"),
        api="decisions" if kind == "openrouter" and stored.get("api") == "decisions" else "chat",
        typesafe_key=key if kind == "typesafe" else "",
        base_url=base_url,
        api_key=key if kind in VIZ_KEYED_KINDS else "",
        allow_remote=consent or (local and destination is not None),
        destination_id=destination,
        revision=int(stored.get("revision", 1)),
    )


def viz_decision_discovery_config(
    config: AIExecutionConfig, api_key: str, settings: Settings
) -> AIExecutionConfig:
    """Pure-ish: execution config pointed at the fixed OpenRouter endpoint with ``api_key`` for model discovery.

    The endpoint goes through the same deployment host policy as a saved router (422 when denied), so discovery
    cannot reach a host the owner's policy forbids. The key is used for this call only.
    """
    endpoint = _endpoint(VIZ_OPENROUTER_URL, settings)
    return config.model_copy(
        update={
            "gateway_base_url": endpoint,
            "gateway_api_key": api_key,
            "gateway_kind": "openrouter",
            "endpoint_destination_id": _identity(endpoint, "")[1],
            "gateway_credential_configured": bool(api_key),
            "endpoint_policy_denied": endpoint is None,
        }
    )


def viz_router_gateway_config(
    config: AIExecutionConfig, resolved: VizRouterResolved
) -> tuple[AIExecutionConfig, ModelMapping]:
    """Pure: the execution config pointed at the router's provider, plus the router's model mapping.

    ``derive_for_mapping`` selects the router provider's endpoint, key and destination; remote providers are
    marked ``destination="remote"`` for the gateway privacy policy. The router has no role slot in the defaults.
    Probe and runtime share a nonsecret cache identity bound to the base configuration and router selection,
    revision, API, endpoint and credential fingerprint; changing the router invalidates only its evidence.
    """
    # Router saves deliberately leave the base AI revision unchanged, so its evidence needs a separate identity.
    identity = _fingerprint(
        json.dumps(
            (
                config.gateway_identity,
                resolved.model_dump(exclude={"typesafe_key", "api_key"}),
                _fingerprint(resolved.api_key),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # Router evidence follows the same split: its model identity derives from the base model identity.
    model_identity = _fingerprint(
        json.dumps(
            (
                config.model_identity,
                resolved.model_dump(exclude={"typesafe_key", "api_key"}),
                _fingerprint(resolved.api_key),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    config = config.model_copy(
        update={"gateway_identity": identity, "model_identity": model_identity}
    )
    if resolved.kind in VIZ_KEYED_KINDS:
        # Self-contained kind: its own validated endpoint and key replace the provider's; never the owner's providers.
        own = ModelMapping(model=resolved.model, destination="remote")
        return config.model_copy(
            update={
                "gateway_base_url": resolved.base_url,
                "gateway_api_key": resolved.api_key,
                "gateway_kind": "openrouter" if resolved.kind == "openrouter" else "custom",
                "endpoint_destination_id": resolved.destination_id,
                "gateway_credential_configured": bool(resolved.api_key),
                "endpoint_policy_denied": resolved.base_url is None,
            }
        ), own
    provider = config.providers.get(resolved.provider_id or DEFAULT_PROVIDER_ID)
    mapping = ModelMapping(
        model=resolved.model,
        provider_id=resolved.provider_id,
        destination="unknown" if provider is None or provider.local else "remote",
    )
    return derive_for_mapping(config, mapping), mapping


async def read_viz_router(
    session: AsyncSession, settings: Settings, *, scope: Scope
) -> VizRouterRead:
    """Return the router settings projection for the owner workspace: key presence only, never the key."""
    await require_settings_scope(
        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled
    )
    row = await _row(session, scope=scope)
    resolved = await resolve_viz_router(session, settings, scope=scope)
    return VizRouterRead.model_validate(
        {
            "configuration_revision": resolved.revision,
            "kind": resolved.kind,
            "provider_id": resolved.provider_id,
            "model": resolved.model,
            "mode": resolved.mode,
            "api": resolved.api,
            "typesafe_key_configured": bool(_viz_stored(row).get("typesafe_key_ciphertext")),
            "openrouter_key_configured": bool(_viz_stored(row).get("openrouter_key_ciphertext")),
            "cloudflare_key_configured": bool(_viz_stored(row).get("cloudflare_key_ciphertext")),
            "account_id": _viz_stored(row).get("account_id"),
            "gateway_url": _viz_stored(row).get("gateway_url"),
            "allow_remote_visualization_routing": resolved.allow_remote,
            "destination_id": resolved.destination_id,
        }
    )


def _apply_viz_kind_fields(
    stored: dict[str, Any],
    update: VizRouterUpdate,
    providers: dict[str, dict[str, Any]],
    settings: Settings,
) -> None:
    """Add the kind-specific router fields to ``stored`` after validating them (mutates ``stored``).

    ``llm`` needs an existing provider and a model from that provider's discovered ``decision`` (or
    legacy ``llm``) list. Keyed kinds (openrouter, cloudflare) need a model; openrouter may select the
    Decisions API and cloudflare carries a normalized account id and gateway URL. The resulting
    endpoint is validated against deployment policy. Raises ``ValidationFailedError`` (422).
    """
    if update.kind == "llm":
        pid = update.provider_id or DEFAULT_PROVIDER_ID
        record = providers.get(pid)
        if record is None:
            raise ValidationFailedError(detail="Unknown model provider")
        if not any(m.id == update.model and m.type in {"decision", "llm"} for m in _models(record)):
            raise ValidationFailedError(
                detail="Choose a decision or language model discovered on this provider"
            )
        stored.update(provider_id=pid, model=update.model, mode=update.mode)
    if update.kind in VIZ_KEYED_KINDS:
        if not update.model.strip():
            raise ValidationFailedError(detail="A router model is required")
        stored.update(model=update.model.strip(), mode=update.mode)
        if update.kind == "openrouter" and update.api == "decisions":
            stored["api"] = "decisions"  # only openrouter has the Decisions API; omitted => chat
        if update.kind == "cloudflare":
            stored.update(
                account_id=(update.account_id or "").strip().lower() or None,
                gateway_url=(update.gateway_url or "").strip() or None,
            )
        _viz_endpoint(
            stored, settings
        )  # 422 on bad account id / non-https / deployment endpoint policy


def _viz_key_ciphertexts(
    update: VizRouterUpdate,
    previous: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Return the per-kind key ciphertexts after applying this save's credential action.

    Starts from the stored ciphertext of every kind; for the selected kind a ``replaced`` action
    encrypts the new key (required) and ``removed`` drops it. A keyed kind must end with a key.
    Raises ``ValidationFailedError`` (422).
    """
    ciphertexts = {
        name: previous.get(f"{name}_key_ciphertext") for name in ("typesafe", *VIZ_KEYED_KINDS)
    }
    if update.kind == "typesafe":
        action, value, label = update.typesafe_key_action, update.typesafe_key, "TypeSafe"
    else:
        action, value, label = update.api_key_action, update.api_key, update.kind
    if update.kind in {"typesafe", *VIZ_KEYED_KINDS}:
        if action == "replaced":
            if not value:
                raise ValidationFailedError(detail=f"Replacement {label} key is required")
            ciphertexts[update.kind] = _cipher(settings).encrypt(value.encode()).decode()
        elif action == "removed":
            ciphertexts[update.kind] = None
    if update.kind in VIZ_KEYED_KINDS and not ciphertexts[update.kind]:
        raise ValidationFailedError(detail=f"A {label} API key is required")
    return ciphertexts


async def save_viz_router(
    session: AsyncSession,
    update: VizRouterUpdate,
    settings: Settings,
    *,
    scope: Scope,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> VizRouterRead:
    """Revision-checked replace of the visualization router settings (own ``revision`` inside the JSON).

    ``llm`` needs an existing provider and a model from that provider's discovered ``decision`` (or legacy ``llm``) list; ``typesafe``, ``openrouter`` and ``cloudflare`` encrypt their own key like AI credentials; openrouter/cloudflare also need a
    model and key and validate their endpoint against deployment policy. Consent is stored bound to the destination chosen in this save and is
    dropped for ``internal``. Never bumps ``configuration_revision`` (that would fail in-flight AI runs).
    """
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
    previous = _viz_stored(row)
    if int(previous.get("revision", 1)) != update.expected_revision:
        raise ConflictError(detail="Visualization router settings changed; reload before saving")
    providers = _stored_providers(row)
    stored: dict[str, Any] = {"kind": update.kind, "revision": int(previous.get("revision", 1)) + 1}
    _apply_viz_kind_fields(stored, update, providers, settings)
    ciphertexts = _viz_key_ciphertexts(update, previous, settings)
    for name, ciphertext in ciphertexts.items():
        if (
            ciphertext
        ):  # per-kind: a key is never reused for another kind's host; only "removed" drops it
            stored[f"{name}_key_ciphertext"] = ciphertext
    destination = _viz_destination(stored, providers, settings)
    if update.kind != "internal" and update.allow_remote_visualization_routing and destination:
        stored["allow_remote_visualization_routing"] = True
        stored["consent_destination"] = destination
    row.viz_router = stored
    await session.flush()
    return await read_viz_router(session, settings, scope=scope)
