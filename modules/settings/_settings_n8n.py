"""n8n integration settings read/resolve/save."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
    DomainError,
    ValidationFailedError,
)
from core.model_gateway.transport import approved_transport
from core.workspaces.schemas import Scope
from modules.settings._settings_common import (
    _cipher,
    _decrypt,
    _endpoint,
    _row,
    require_settings_scope,
    scope_actor,
)
from modules.settings.models import (
    AISettingsRecord,
)
from modules.settings.schemas import (
    N8nRead,
    N8nUpdate,
)


class N8nResolved(BaseModel):
    """Effective n8n connection: stored value first, ``N8N_*`` environment otherwise. Secrets stay out of repr."""

    service_url: str
    api_key: str = Field(default="", repr=False)
    webhook_token: str = Field(default="", repr=False)
    service_url_source: str = "environment"
    api_key_source: str = "none"
    webhook_token_source: str = "none"  # noqa: S105  # label/URL/enum value, not a credential
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
        query = (
            query.outerjoin(Owner, Owner.id == AISettingsRecord.owner_id)
            .where(AISettingsRecord.n8n != text("'{}'::jsonb"))
            .order_by(
                (AISettingsRecord.workspace_id == Owner.default_workspace_id).desc(),
                AISettingsRecord.workspace_id,
                AISettingsRecord.owner_id,
            )
        )
    stored: dict[str, Any] = await session.scalar(query.limit(1)) or {}
    return stored


async def n8n_enabled(session: AsyncSession, *, workspace_id: UUID | None = None) -> bool:
    """Return the n8n kill switch (absent flag means enabled) without decrypting any secret.

    Plain read for ingress guards that have no ``Settings`` at hand; ``workspace_id`` None follows the
    worker pick used by ``resolve_n8n``.
    """
    return bool((await _n8n_stored(session, workspace_id)).get("enabled", True))


async def resolve_n8n(
    session: AsyncSession, settings: Settings, *, workspace_id: UUID | None = None
) -> N8nResolved:
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
        """Resolve one n8n secret as ``(value, source)``: the decrypted stored value first, else the environment value when allowed, else empty. An undecryptable stored value falls through to the environment."""
        ciphertext = stored.get(f"{name}_ciphertext")
        if ciphertext:
            try:
                return _decrypt(ciphertext, settings), "database"
            except (DomainError, HTTPException):
                pass
        return (env, "environment") if env and env_ok else ("", "none")

    key, key_source = pick("api_key", settings.n8n_api_key.get_secret_value())
    token, token_source = pick("webhook_token", settings.n8n_webhook_token.get_secret_value())
    return N8nResolved(
        service_url=url or str(settings.n8n_service_url),
        service_url_source="database" if url else "environment",
        api_key=key,
        api_key_source=key_source,
        webhook_token=token,
        webhook_token_source=token_source,
        enabled=bool(stored.get("enabled", True)),
    )


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
    transport = approved_transport(
        url, settings.ai_allowed_endpoint_cidrs, allow_global=settings.ai_allow_public_providers
    )
    return httpx.AsyncClient(
        transport=transport, timeout=timeout, trust_env=False, follow_redirects=False
    )


async def read_n8n(session: AsyncSession, settings: Settings, *, scope: Scope) -> N8nRead:
    """Return n8n settings for the owner workspace: configured flags and sources, never secrets."""
    await require_settings_scope(
        session, scope=scope, multi_workspace_enabled=settings.multi_workspace_enabled
    )
    row = await _row(session, scope=scope)
    from modules.connectors.public import n8n_callback_base

    resolved = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    return N8nRead(
        configuration_revision=_n8n_revision(row),
        service_url=resolved.service_url,
        api_key_configured=bool(resolved.api_key),
        webhook_token_configured=bool(resolved.webhook_token),
        service_url_source=resolved.service_url_source,
        api_key_source=resolved.api_key_source,
        webhook_token_source=resolved.webhook_token_source,
        enabled=resolved.enabled,
        callback_base_url=n8n_callback_base(),
    )


async def save_n8n(
    session: AsyncSession,
    update: N8nUpdate,
    settings: Settings,
    *,
    scope: Scope,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> N8nRead:
    """Revision-checked replace of the stored n8n URL; secrets change only by their ``*_action``.

    ``service_url`` None clears the stored URL. ``enabled`` None keeps the stored kill switch (absent =
    enabled); it changes under the same revision check and never touches n8n workflows. Secrets are
    Fernet-encrypted like AI credentials.
    Uses its own ``revision`` inside the n8n JSON and never bumps ``configuration_revision``
    (that would fail in-flight AI runs).
    """
    service_url = n8n_service_url(update.service_url, settings)
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
    if _n8n_revision(row) != update.expected_revision:
        raise ConflictError(detail="n8n settings changed; reload before saving")
    stored = dict(row.n8n or {})
    stored.pop("service_url", None)
    if service_url:
        stored["service_url"] = service_url
    for name, action, value in (
        ("api_key", update.api_key_action, update.api_key),
        ("webhook_token", update.webhook_token_action, update.webhook_token),
    ):
        if action == "replaced":
            if not value:
                raise ValidationFailedError(
                    detail=f"Replacement n8n {name.replace('_', ' ')} is required"
                )
            stored[f"{name}_ciphertext"] = _cipher(settings).encrypt(value.encode()).decode()
        elif action == "removed":
            stored.pop(f"{name}_ciphertext", None)
    if update.enabled is not None:
        stored["enabled"] = update.enabled
    stored["revision"] = _n8n_revision(row) + 1
    row.n8n = stored
    await session.flush()
    return await read_n8n(session, settings, scope=scope)
