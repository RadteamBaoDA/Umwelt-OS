from typing import Annotated
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.model_gateway.client import ModelGateway, ModelGatewayError, PrivacyPolicyDenied
from core.config import Settings
from core.database import get_session
from core.model_gateway.schemas import AISettingsRead, AISettingsUpdate, ConnectionDraft, ModelMapping, ModelSettingsRead, PrivacySettings
from modules.settings import models, public
from modules.settings.schemas import (
    ModuleLifecycleRead, ModuleLifecycleUpdate, OwnerPreferencesRead, OwnerPreferencesUpdate,
    RetentionSettingsRead, RetentionSettingsUpdate,
)
from modules.settings import lifecycle

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.get("/preferences", response_model=OwnerPreferencesRead)
async def read_owner_preferences(session: Session, _owner: OwnerRead) -> OwnerPreferencesRead:
    """Read owner preferences after the read-scope dependency authorizes access."""
    return await public.read_owner_preferences(session)


@router.put("/preferences", response_model=OwnerPreferencesRead)
async def save_owner_preferences(
    value: OwnerPreferencesUpdate,
    session: Session,
    _owner: OwnerWrite,
) -> OwnerPreferencesRead:
    """Save preferences for an authorized writer and commit the update."""
    saved = await public.save_owner_preferences(session, value)
    await session.commit()
    return saved


@router.get("/retention", response_model=RetentionSettingsRead)
async def read_retention(session: Session, _owner: OwnerRead) -> RetentionSettingsRead:
    """Read the owner's trace-retention policy after owner authorization."""
    return await lifecycle.read_retention_settings(session)


@router.patch("/retention", response_model=RetentionSettingsRead)
async def patch_retention(value: RetentionSettingsUpdate, session: Session, _owner: OwnerWrite) -> RetentionSettingsRead:
    """Save a revision-fenced trace-retention policy; source and document history stay retained."""
    result = await lifecycle.save_retention_settings(session, value)
    await session.commit()
    return result


@router.get("/modules", response_model=ModuleLifecycleRead)
async def read_modules(session: Session, _owner: OwnerRead) -> ModuleLifecycleRead:
    """Read descriptor-derived module availability for owner controls."""
    return await lifecycle.read_module_lifecycle(session)


@router.patch("/modules", response_model=ModuleLifecycleRead)
async def patch_module(value: ModuleLifecycleUpdate, session: Session, request: Request, _owner: OwnerWrite) -> ModuleLifecycleRead:
    """Persist one dependency-aware module toggle and refresh this API process after commit."""
    result = await lifecycle.save_module_lifecycle(session, value)
    await session.commit()
    lifecycle.sync_application_modules(request.app, result)
    return result


async def _read(session: AsyncSession, request: Request) -> AISettingsRead:
    """Build the AI settings response and attach gateway-matched capabilities."""
    value = await public.read_ai_settings(session, request.app.state.settings, request.app.state.redis)
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    value.capabilities = await models.list_capabilities(request.app.state.redis, value.aliases, config.gateway_identity)
    return value


@router.get("/ai", response_model=AISettingsRead)
async def read_ai(session: Session, request: Request, _owner: OwnerRead) -> AISettingsRead:
    """Return authorized AI settings with cached capability status."""
    return await _read(session, request)


@router.put("/ai", response_model=AISettingsRead)
async def save_ai(value: AISettingsUpdate, session: Session, request: Request, _owner: OwnerWrite) -> AISettingsRead:
    """Save AI settings, commit, then report capabilities for the saved gateway."""
    saved = await public.save_ai_settings(session, value, request.app.state.settings)
    await session.commit()
    saved.capabilities = await models.list_capabilities(request.app.state.redis, saved.aliases,
        (await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)).gateway_identity)
    return saved


@router.post("/ai/discover")
async def discover_models(body: ConnectionDraft, request: Request, session: Session, _owner: OwnerWrite) -> dict[str, list[str]]:
    """Discover models using a validated draft endpoint and explicit credential.

    The gateway rechecks endpoint policy before sending; upstream gateway errors
    become HTTP 502 and missing draft credentials produce HTTP 409.
    """
    endpoint = public.validate_endpoint(str(body.base_url), request.app.state.settings)
    if endpoint is None:
        raise HTTPException(status_code=422, detail="Gateway endpoint is required")
    credential = body.api_key
    if not credential:
        raise HTTPException(status_code=409, detail="Configure a gateway credential first")
    async def recheck_send() -> None:
        """Reapply deployment endpoint policy immediately before network access."""
        public.validate_endpoint(endpoint, request.app.state.settings)

    gateway = ModelGateway(request.app.state.redis, endpoint, credential,
        "omniroute", 20,
        gateway_identity=hashlib.sha256(endpoint.encode()).hexdigest(), before_send=recheck_send,
        approved_endpoint_cidrs=tuple(request.app.state.settings.ai_allowed_endpoint_cidrs))
    try:
        return {"model_ids": await gateway.discover_models()}
    except ModelGatewayError as exc:
        raise HTTPException(status_code=502, detail="Model discovery failed") from exc


@router.get("/models", response_model=ModelSettingsRead)
async def read_models(session: Session, request: Request, _owner: OwnerRead) -> ModelSettingsRead:
    """Return model aliases and capability status without exposing credentials."""
    value = await _read(session, request)
    return ModelSettingsRead(aliases=value.aliases, capabilities=value.capabilities,
        credential_configured=value.omniroute_credential_configured)


@router.patch("/models", response_model=ModelSettingsRead)
async def patch_models(values: dict[str, ModelMapping], session: Session, request: Request, _owner: OwnerWrite) -> ModelSettingsRead:
    """Merge valid alias changes into current settings using revision checking."""
    current = await _read(session, request)
    if not values or any(alias not in models.ALIASES for alias in values):
        raise HTTPException(status_code=422, detail="Unknown or empty model alias mapping")
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    update = AISettingsUpdate(expected_revision=current.configuration_revision,
        omniroute_base_url=config.omniroute_base_url, omniroute_credential_action="unchanged",
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        chat_alias=config.chat_alias, brief_alias=config.brief_alias, aliases={**config.aliases, **values},
        privacy=config.privacy, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings)
    await session.commit()
    return ModelSettingsRead(aliases=saved.aliases, capabilities=[], credential_configured=saved.omniroute_credential_configured)


@router.get("/privacy", response_model=PrivacySettings)
async def read_privacy(session: Session, request: Request, _owner: OwnerRead) -> PrivacySettings:
    """Return the privacy grants bound to the current AI destinations."""
    return (await _read(session, request)).privacy


@router.patch("/privacy", response_model=PrivacySettings)
async def patch_privacy(value: PrivacySettings, session: Session, request: Request, _owner: OwnerWrite) -> PrivacySettings:
    """Update destination-bound privacy grants through the shared CAS save path."""
    current = await _read(session, request)
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    update = AISettingsUpdate(expected_revision=current.configuration_revision,
        omniroute_base_url=config.omniroute_base_url, web_search_provider=config.web_search_provider,
        web_search_endpoint=config.web_search_endpoint, chat_alias=config.chat_alias, brief_alias=config.brief_alias,
        aliases=config.aliases, privacy=value, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings)
    await session.commit()
    return saved.privacy
