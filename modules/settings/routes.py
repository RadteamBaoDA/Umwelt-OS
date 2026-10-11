import hashlib
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_account, require_account_write
from core.auth.public import authenticated_session_ref
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from core.database import get_session
from core.model_gateway.client import ModelGateway, ModelGatewayError
from core.model_gateway.transport import EndpointNetworkPolicyError, approved_web_search_transport
from core.model_gateway.schemas import (
    AISettingsRead,
    AISettingsUpdate,
    ConnectionDraft,
    ModelMapping,
    ModelSettingsRead,
    PrivacySettings,
)
from modules.chat import web_search as web
from modules.settings import lifecycle, models, public
from modules.settings.schemas import (
    ConnectionTestResult,
    N8nRead,
    N8nTest,
    N8nUpdate,
    ModuleLifecycleRead,
    ModuleLifecycleUpdate,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
    SystemPromptsRead,
    SystemPromptsUpdate,
    TranslationSettingsRead,
    TranslationSettingsUpdate,
    WebSearchRead,
    WebSearchTest,
    WebSearchUpdate,
)
from modules.translations import public as translations

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


@router.get("/translation", response_model=TranslationSettingsRead)
async def read_translation(request: Request, session: Session, member: OwnerRead) -> TranslationSettingsRead:
    """Read-only workspace translation projection; any member may read it, none can write it."""
    return await translations.read_translation_settings(
        session, scope=member, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.patch("/translation", response_model=TranslationSettingsRead)
async def patch_translation(value: TranslationSettingsUpdate, request: Request, session: Session, _owner: OwnerWrite) -> TranslationSettingsRead:
    """Owner-only CAS update; 409 on stale revision, 403 for members."""
    result = await translations.save_translation_settings(
        session, value, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),))
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
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner)
    credential, local, destination = public.resolve_draft_provider(config, endpoint, body.api_key, body.provider_id, body.kind)
    if not credential and not local:
        raise HTTPException(status_code=409, detail="Configure a gateway credential first")
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
        destination, 20,
        scope=_owner, configuration_revision=0,
        gateway_identity=hashlib.sha256((config.gateway_identity + endpoint + hashlib.sha256(credential.encode()).hexdigest()).encode()).hexdigest(), before_send=recheck_send,
        approved_endpoint_cidrs=tuple(request.app.state.settings.ai_allowed_endpoint_cidrs),
        allow_public_endpoints=request.app.state.settings.ai_allow_public_providers)
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
        credential_configured=any(p.credential_configured for p in value.providers))


@router.patch("/models", response_model=ModelSettingsRead)
async def patch_models(values: dict[str, ModelMapping], session: Session, request: Request, _owner: OwnerWrite) -> ModelSettingsRead:
    """Merge valid alias changes into current settings using revision checking."""
    current = await _read(session, request, scope=_owner)
    if not values or any(alias not in models.ALIASES for alias in values):
        raise HTTPException(status_code=422, detail="Unknown or empty model alias mapping")
    config = await public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis, scope=_owner)
    update = AISettingsUpdate(expected_revision=current.configuration_revision,
        web_search_provider=config.web_search_provider, web_search_endpoint=config.web_search_endpoint,
        chat_alias=config.chat_alias, brief_alias=config.brief_alias, aliases={**config.aliases, **values},
        privacy=config.privacy, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings, scope=_owner, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return ModelSettingsRead(aliases=saved.aliases, capabilities=[], credential_configured=any(p.credential_configured for p in saved.providers))


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
        web_search_provider=config.web_search_provider,
        web_search_endpoint=config.web_search_endpoint, chat_alias=config.chat_alias, brief_alias=config.brief_alias,
        aliases=config.aliases, privacy=value, request_timeout_seconds=config.request_timeout_seconds)
    saved = await public.save_ai_settings(session, update, request.app.state.settings, scope=_owner, auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved.privacy


@router.get("/prompts", response_model=SystemPromptsRead)
async def read_prompts(session: Session, request: Request, _owner: OwnerRead) -> SystemPromptsRead:
    """Return editable system prompts with their built-in defaults."""
    return await public.read_system_prompts(session, request.app.state.settings, request.app.state.redis, scope=_owner)


@router.put("/prompts", response_model=SystemPromptsRead)
async def save_prompts(value: SystemPromptsUpdate, session: Session, request: Request, _owner: OwnerWrite) -> SystemPromptsRead:
    """Save prompt overrides under the shared revision check; null/blank resets to default."""
    saved = await public.save_system_prompts(session, value, request.app.state.settings, scope=_owner,
                                             auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved


@router.get("/web-search", response_model=WebSearchRead)
async def read_web_search(session: Session, request: Request, _owner: OwnerRead) -> WebSearchRead:
    """Return web-search provider, endpoint, key presence and remote-search consent; never the key."""
    return await public.read_web_search(session, request.app.state.settings, request.app.state.redis, scope=_owner)


@router.put("/web-search", response_model=WebSearchRead)
async def save_web_search(value: WebSearchUpdate, session: Session, request: Request, _owner: OwnerWrite) -> WebSearchRead:
    """Save web-search settings under the shared revision check (409 when stale)."""
    saved = await public.save_web_search(session, value, request.app.state.settings, scope=_owner,
                                         auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved


@router.post("/web-search/test", response_model=ConnectionTestResult)
async def test_web_search(body: WebSearchTest, session: Session, request: Request, _owner: OwnerWrite) -> ConnectionTestResult:
    """Run one fixed query against a draft provider; a blank key reuses the stored one only for the stored endpoint."""
    settings = request.app.state.settings
    endpoint = public.validate_endpoint(str(body.endpoint), settings)
    config = await public.get_ai_execution_config(session, settings, request.app.state.redis, scope=_owner)
    key = body.api_key or (config.web_search_api_key if config.web_search_endpoint == endpoint else "")
    await session.rollback()  # no SQL held across egress
    if endpoint is None or (body.provider != "searxng" and not key):
        return ConnectionTestResult(ok=False, error="credential_required")
    try:
        transport = approved_web_search_transport(endpoint, settings.ai_allowed_endpoint_hosts,
                                                  settings.web_search_allowed_cidrs, allow_http=body.provider == "searxng")
        results = await web.search(body.provider, endpoint, key, "test", transport=transport)
    except web.WebSearchError as exc:
        return ConnectionTestResult(ok=False, error=exc.code)
    except EndpointNetworkPolicyError:
        return ConnectionTestResult(ok=False, error="network_denied")
    return ConnectionTestResult(ok=True, detail=len(results))


@router.get("/n8n", response_model=N8nRead)
async def read_n8n(session: Session, request: Request, _owner: OwnerRead) -> N8nRead:
    """Return the effective n8n URL and whether a key/webhook token is set (database or environment); never secrets."""
    return await public.read_n8n(session, request.app.state.settings, scope=_owner)


@router.put("/n8n", response_model=N8nRead)
async def save_n8n(value: N8nUpdate, session: Session, request: Request, _owner: OwnerWrite) -> N8nRead:
    """Save n8n URL and encrypted secrets under the shared revision check. A new webhook token needs workflows re-provisioned."""
    saved = await public.save_n8n(session, value, request.app.state.settings, scope=_owner,
                                  auth_sessions=(authenticated_session_ref(request),))
    await session.commit()
    return saved


@router.post("/n8n/test", response_model=ConnectionTestResult)
async def test_n8n(body: N8nTest, session: Session, request: Request, _owner: OwnerWrite) -> ConnectionTestResult:
    """Call n8n ``GET /api/v1/workflows?limit=1``; blanks use the effective value, a stored key only for its own URL."""
    settings = request.app.state.settings
    resolved = await public.resolve_n8n(session, settings, workspace_id=_owner.workspace_id)
    url = public.n8n_service_url(body.service_url, settings) or resolved.service_url.rstrip("/")
    key = body.api_key or (resolved.api_key if url == resolved.service_url.rstrip("/") else "")
    await session.rollback()
    if not key:
        return ConnectionTestResult(ok=False, error="credential_required")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.get(f"{url}/api/v1/workflows", params={"limit": 1}, headers={"X-N8N-API-KEY": key})
    except httpx.HTTPError:
        return ConnectionTestResult(ok=False, error="unreachable")
    if response.is_success:
        return ConnectionTestResult(ok=True, detail=response.status_code)
    return ConnectionTestResult(ok=False, error="unauthorized" if response.status_code in (401, 403) else "http_error",
                                detail=response.status_code)
