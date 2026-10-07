import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.public import authenticated_session_ref
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from core.database import get_session
from core.model_gateway.client import ModelGateway, ModelGatewayError
from core.model_gateway.schemas import (
    AISettingsRead,
    AISettingsUpdate,
    ConnectionDraft,
    ModelMapping,
    ModelSettingsRead,
    PrivacySettings,
)
from modules.settings import lifecycle, models, public
from modules.settings.schemas import (
    ModuleLifecycleRead,
    ModuleLifecycleUpdate,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
AccountRead = Annotated[object, Depends(require_account)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
AccountWrite = Annotated[object, Depends(require_account_write)]


@router.get("/preferences", response_model=OwnerPreferencesRead)
async def read_owner_preferences(request: Request, session: Session, _owner: AccountRead) -> OwnerPreferencesRead:
    """Read active account preferences; selected workspace and member role are irrelevant."""
    return await public.read_owner_preferences(session, actor_user_id=request.state.account.id,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.put("/preferences", response_model=OwnerPreferencesRead)
async def save_owner_preferences(
    value: OwnerPreferencesUpdate,
    session: Session,
    _owner: AccountWrite,
    request: Request,
) -> OwnerPreferencesRead:
    """Commit active account preferences under exact-session auth/CSRF/backup admission and CAS."""
    saved = await public.save_owner_preferences(session, value, actor_user_id=request.state.account.id,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved


@router.get("/retention", response_model=RetentionSettingsRead)
async def read_retention(request: Request, session: Session, _owner: OwnerRead) -> RetentionSettingsRead:
    """Read the owner's trace-retention policy after owner authorization."""
    return await lifecycle.read_retention_settings(session, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.patch("/retention", response_model=RetentionSettingsRead)
async def patch_retention(value: RetentionSettingsUpdate, request: Request, session: Session, _owner: OwnerWrite) -> RetentionSettingsRead:
    """Save a revision-fenced trace-retention policy; source and document history stay retained."""
    result = await lifecycle.save_retention_settings(session, value, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return result


@router.get("/modules", response_model=ModuleLifecycleRead)
async def read_modules(request: Request, session: Session, _owner: OwnerRead) -> ModuleLifecycleRead:
    """Read descriptor-derived module availability for owner controls."""
    return await lifecycle.read_module_lifecycle(session, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.patch("/modules", response_model=ModuleLifecycleRead)
async def patch_module(value: ModuleLifecycleUpdate, session: Session, request: Request, _owner: OwnerWrite) -> ModuleLifecycleRead:
    """Commit workspace-only dependency-aware toggle; process capability registry is immutable."""
    result = await lifecycle.save_module_lifecycle(session, value, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return result


async def _read(session: AsyncSession, request: Request, *, scope: WorkspaceContext) -> AISettingsRead:
    """Build the AI settings response and attach gateway-matched capabilities."""
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=scope)
    value = public._read(config)
    value.capabilities = await models.list_capabilities(request.app.state.redis, config=config, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    return value


@router.get("/ai", response_model=AISettingsRead)
async def read_ai(session: Session, request: Request, _owner: OwnerRead) -> AISettingsRead:
    """Return authorized AI settings with cached capability status."""
    return await _read(session, request, scope=_owner)


@router.put("/ai", response_model=AISettingsRead)
async def save_ai(value: AISettingsUpdate, session: Session, request: Request, _owner: OwnerWrite) -> AISettingsRead:
    """Save AI settings, commit, then report capabilities for the saved gateway."""
    saved = await public.save_ai_settings(session, value, request.app.state.settings, scope=_owner, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    saved.capabilities = await models.list_capabilities(request.app.state.redis, config=
        await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner),
        scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
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
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner)
    session_ref = authenticated_session_ref(request)
    await session.rollback()
    async def recheck_send() -> None:
        """Fresh exact-session/access/config check; always release SQL before discovery HTTP."""
        try:
            await public.check_ai_execution_config(session, request.app.state.settings, request.app.state.redis,
                                                   scope=_owner, expected=config, auth_sessions=(session_ref,))
            public.validate_endpoint(endpoint, request.app.state.settings)
        finally:
            await session.rollback()

    gateway = ModelGateway(request.app.state.redis, endpoint, credential,
        "omniroute", 20,
        scope=_owner, configuration_revision=0,
        gateway_identity=hashlib.sha256((config.gateway_identity + endpoint + hashlib.sha256(credential.encode()).hexdigest()).encode()).hexdigest(), before_send=recheck_send,
        approved_endpoint_cidrs=tuple(request.app.state.settings.ai_allowed_endpoint_cidrs))
    try:
        model_ids = await gateway.discover_models()
        await recheck_send()
        return {"model_ids": model_ids}
    except ModelGatewayError as exc:
        raise HTTPException(status_code=502, detail="Model discovery failed") from exc


@router.get("/models", response_model=ModelSettingsRead)
async def read_models(session: Session, request: Request, _owner: OwnerRead) -> ModelSettingsRead:
    """Return model aliases and capability status without exposing credentials."""
    value = await _read(session, request, scope=_owner)
    return ModelSettingsRead(aliases=value.aliases, capabilities=value.capabilities,
        credential_configured=value.omniroute_credential_configured)


@router.patch("/models", response_model=ModelSettingsRead)
async def patch_models(values: dict[str, ModelMapping], session: Session, request: Request, _owner: OwnerWrite) -> ModelSettingsRead:
    """Merge valid alias changes into current settings using revision checking."""
    current = await _read(session, request, scope=_owner)
    if not values or any(alias not in models.ALIASES for alias in values):
        raise HTTPException(status_code=422, detail="Unknown or empty model alias mapping")
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner)
    update = AISettingsUpdate(expected_revision=current.configuration_revision,
        omniroute_base_url=config.omniroute_base_url, omniroute_credential_action="unchanged",
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        chat_alias=config.chat_alias, brief_alias=config.brief_alias, aliases={**config.aliases, **values},
        privacy=config.privacy, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings, scope=_owner, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return ModelSettingsRead(aliases=saved.aliases, capabilities=[], credential_configured=saved.omniroute_credential_configured)


@router.get("/privacy", response_model=PrivacySettings)
async def read_privacy(session: Session, request: Request, _owner: OwnerRead) -> PrivacySettings:
    """Return the privacy grants bound to the current AI destinations."""
    return (await _read(session, request, scope=_owner)).privacy


@router.patch("/privacy", response_model=PrivacySettings)
async def patch_privacy(value: PrivacySettings, session: Session, request: Request, _owner: OwnerWrite) -> PrivacySettings:
    """Update destination-bound privacy grants through the shared CAS save path."""
    current = await _read(session, request, scope=_owner)
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner)
    update = AISettingsUpdate(expected_revision=current.configuration_revision,
        omniroute_base_url=config.omniroute_base_url, web_search_provider=config.web_search_provider,
        web_search_endpoint=config.web_search_endpoint, chat_alias=config.chat_alias, brief_alias=config.brief_alias,
        aliases=config.aliases, privacy=value, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings, scope=_owner, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved.privacy
