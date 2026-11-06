"""Web-search target read/save."""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
    ValidationFailedError,
)
from core.workspaces.schemas import Scope
from modules.settings._settings.common import (
    _cipher,
    _endpoint,
    _row,
    _web_destination,
    require_settings_scope,
    scope_actor,
)
from modules.settings._settings.execution import get_ai_execution_config
from modules.settings.models import (
    AISettingsRecord,
)
from modules.settings.schemas import (
    WebSearchEntryRead,
    WebSearchRead,
    WebSearchUpdate,
)


async def read_web_search(
    session: AsyncSession, settings: Settings, redis: Redis | None = None, *, scope: Scope
) -> WebSearchRead:
    """Return ordered owner search destinations without exposing credential values."""
    config = await get_ai_execution_config(session, settings, redis, scope=scope)
    targets = []
    if config.web_search_provider != "none" and config.web_search_endpoint:
        targets.append(
            WebSearchEntryRead(
                provider=config.web_search_provider,
                endpoint=config.web_search_endpoint,
                credential_configured=config.web_search_credential_configured,
                destination_id=_web_destination(config.web_search_endpoint),
            )
        )
    targets.extend(
        WebSearchEntryRead(
            provider=t.provider,
            endpoint=t.endpoint,
            credential_configured=t.credential_configured,
            destination_id=_web_destination(t.endpoint),
        )
        for t in config.web_search_fallbacks
    )
    return WebSearchRead(
        configuration_revision=config.configuration_revision,
        providers=targets,
        allow_remote_web_search=config.privacy.allow_remote_web_search,
    )


async def save_web_search(
    session: AsyncSession,
    update: WebSearchUpdate,
    settings: Settings,
    *,
    scope: Scope,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> WebSearchRead:
    """Replace ordered search targets and destination-bound consent under the owner revision fence.

    Keys are retained only for the same normalized endpoint, regardless of ordering; new keys are encrypted.
    The caller owns the transaction, and duplicate endpoints and missing required keys are rejected.
    """
    endpoints = [_endpoint(str(item.endpoint), settings) for item in update.providers]
    if any(endpoint is None for endpoint in endpoints) or len(set(endpoints)) != len(endpoints):
        raise ValidationFailedError(detail="Web search endpoints must be present and unique")
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
    stored = {row.web_search_endpoint: row.web_search_api_key_ciphertext}
    stored.update(
        {
            item.get("endpoint"): item.get("key_ciphertext")
            for item in (row.web_search_fallbacks or [])
        }
    )
    records = []
    for item, endpoint in zip(update.providers, endpoints, strict=True):
        ciphertext = stored.get(endpoint)
        if item.credential_action == "replaced":
            if not item.api_key:
                raise ValidationFailedError(detail="Replacement search credential is required")
            ciphertext = _cipher(settings).encrypt(item.api_key.encode()).decode()
        elif item.credential_action == "removed":
            ciphertext = None
        if item.provider in ("tavily", "brave", "omniroute") and not ciphertext:
            raise ValidationFailedError(detail="Search credential is required for this provider")
        records.append(
            {"provider": item.provider, "endpoint": endpoint, "key_ciphertext": ciphertext}
        )
    if records:
        row.web_search_provider = update.providers[0].provider
        row.web_search_endpoint = endpoints[0]
        row.web_search_api_key_ciphertext = records[0]["key_ciphertext"]
    else:
        row.web_search_provider, row.web_search_endpoint, row.web_search_api_key_ciphertext = (
            "none",
            None,
            None,
        )
    row.web_search_fallbacks = records[1:]
    destinations = [_web_destination(endpoint) for endpoint in endpoints]
    consent = bool(update.allow_remote_web_search and destinations)
    row.privacy = {
        **(row.privacy or {}),
        "allow_remote_web_search": consent,
        "web_search_destinations": destinations if consent else [],
    }
    row.configuration_revision += 1
    await session.flush()
    return await read_web_search(session, settings, scope=scope)
