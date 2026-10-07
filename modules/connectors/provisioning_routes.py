from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, Scope, WorkspaceContext
from core.database import get_session
from core.realtime import commit_with_replay, make_source_change
from modules.connectors import catalog, provisioning, registry
from modules.connectors import public as connector_owner
from modules.connectors.activation import drive_activation, prepare_credential_assignment
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    N8nCredentials,
    decrypt_native_token,
    encrypt_native_token,
    secret_fingerprint,
)
from modules.connectors.models import ConnectorNativeCredential, ConnectorProvisioning
from modules.connectors.n8n import N8nApi
from modules.connectors.public import (
    ConnectorConfig,
    ProviderRateLimited,
    NativeCredentialSnapshot,
    serialize_source_configuration,
    validate_public_url,
)
from modules.ingestion import public as ingestion
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


async def _owner_access(session: AsyncSession, request: Request, scope: WorkspaceContext, *, expected: AccessFence | None = None) -> AccessFence:
    """Reject members and lock original account/session/workspace before domain access.

    Existing write dependencies retain CSRF/backup admission. Capture this fence once;
    later publication and provider callbacks compare it rather than renewing an epoch.
    Caller releases SQL before network and owns final commit/rollback.
    """
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await lock_access_fence(
        session, scope=scope, expected=expected,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),),
    )


async def _validation_send_fence(
    session: AsyncSession, request: Request, source: ConnectorSource, revision: int, *,
    scope: WorkspaceContext, access_fence: AccessFence,
    native_snapshot: NativeCredentialSnapshot | None,
) -> None:
    """Fence every owner Telegram probe against original access/Source/native identity.

    Snapshot None means actual observed absence before I/O. Acquire original exact
    browser admission then Source/provisioning/sorted slots and native; compare every
    retained credential identity field without refreshing the original capture. Release
    SQL before provider HTTP; no token save, health update or permission renewal occurs.
    """
    gate = request.app.state.settings.multi_workspace_enabled
    try:
        await _owner_access(session, request, scope, expected=access_fence)
        current_source, row, _slots = await provisioning.lock_connector(
            session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
            multi_workspace_enabled=gate, expected_access_fence=access_fence,
        )
        if (current_source is None or current_source.status != "active"
                or current_source.workspace_id != source.workspace_id
                or current_source.generation != source.generation
                or (row.desired_revision if row is not None else 0) != revision):
            raise HTTPException(status_code=409, detail="Original Telegram validation scope changed")
        native = await session.scalar(
            select(ConnectorNativeCredential).where(ConnectorNativeCredential.source_id == source.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if native_snapshot is None:
            matches = native is None
        else:
            matches = native is not None and native_snapshot.access_fence == access_fence and all(
                getattr(native, field) == getattr(native_snapshot, field)
                for field in ("source_id", "operation_id", "source_generation", "configuration_revision",
                              "verified_bot_id", "bound_bot_id", "encrypted_token", "state", "validated_at")
            )
        if not matches:
            raise HTTPException(status_code=409, detail="Original Telegram credential changed")
    finally:
        await session.rollback()


class ConnectorSettingsRequest(BaseModel):
    """Validate revisioned connector configuration and auth mode input."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    configuration: ConnectorConfig
    auth_method: Literal["none", "http_header", "telegram_bot_token"] = "none"
    auth_header_name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_auth(self) -> "ConnectorSettingsRequest":
        """Require a header name only when header authentication is selected."""
        if self.auth_method == "http_header" and not self.auth_header_name:
            raise ValueError("auth_header_name is required for header authentication")
        if self.auth_method != "http_header" and self.auth_header_name is not None:
            raise ValueError("auth_header_name requires header authentication")
        return self


class DraftValidationRequest(ConnectorSettingsRequest):
    """Add the source generation required to validate a connector draft."""
    expected_source_generation: int = Field(ge=1)
    secret_action: Literal["keep", "replace"] = "keep"
    secret: SecretStr | None = None

    @model_validator(mode="after")
    def validate_draft_secret(self) -> "DraftValidationRequest":
        """Keep the draft token write-only and apply the same bounded syntax rule as activation."""
        _validate_secret_action(self.secret_action, self.secret)
        return self


class ActivationRequest(BaseModel):
    """Represent a revisioned activation and keep/replace secret action."""
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    secret_action: Literal["keep", "replace"] = "keep"
    secret: SecretStr | None = None

    @model_validator(mode="after")
    def validate_secret_action(self) -> "ActivationRequest":
        """Reject missing replacement secrets and secrets sent with keep."""
        _validate_secret_action(self.secret_action, self.secret)
        return self


def _validate_secret_action(action: str, secret: SecretStr | None) -> None:
    """Require replacement tokens to be nonempty, bounded UTF-8 without whitespace or controls."""
    if action == "keep":
        if secret is not None:
            raise ValueError("secret is only accepted for replacement")
        return
    value = secret.get_secret_value() if secret is not None else ""
    if not 1 <= len(value.encode("utf-8")) <= 512 or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("A valid replacement secret is required")


class ActivationRead(BaseModel):
    """Expose connector activation revisions, state, and recovery support."""
    workspace_id: UUID
    source_id: UUID
    desired_revision: int
    applied_revision: int
    state: str
    error_code: str | None
    credential_recovery: str = "supported"


class ConnectorConfigurationRead(BaseModel):
    """Expose source-scoped connector settings without returning secret values."""
    workspace_id: UUID
    source_id: UUID
    source_type: str
    source_generation: int
    configuration: dict[str, object]
    provider: str | None = None
    expected_revision: int
    auth_method: Literal["none", "http_header", "telegram_bot_token"]
    auth_header_name: str | None
    desired_enabled: bool
    activation_state: str
    activation_error_code: str | None
    provider_credential_configured: bool
    provider_credential_state: str | None


class DraftValidationRead(BaseModel):
    """Report successful draft validation against a source generation."""
    source_id: UUID
    source_generation: int
    expected_revision: int
    validated_at: datetime
    validation_status: Literal["valid"]
    checks: tuple[Literal["configuration", "public_url_policy", "provider_identity", "provider_scope", "receive_mode"], ...]
    verified_bot_id: str | None = Field(default=None, max_length=20)
    scope_verified: bool | None = None


@router.get("/{source_id}/configuration", response_model=ConnectorConfigurationRead)
async def get_configuration(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ConnectorConfigurationRead:
    """Read a managed connector configuration for an authorized owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.type == "mcp":
        row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        configuration = dict(source.configuration or {})
        configuration.setdefault("schedule_interval_minutes", 60)
        configuration.setdefault("timezone", "Asia/Ho_Chi_Minh")
        return ConnectorConfigurationRead(
            workspace_id=source.workspace_id, source_id=source.id, source_type=source.type, provider=source.provider,
            source_generation=source.generation, configuration=configuration,
            expected_revision=row.desired_revision if row is not None else 0,
            auth_method="none", auth_header_name=None,
            desired_enabled=bool(row and row.desired_enabled),
            activation_state=row.state if row is not None else "saved_not_active",
            activation_error_code=row.error_code if row is not None else None,
            provider_credential_configured=False, provider_credential_state=None,
        )
    snapshot = await connector_owner.get_connector_configuration(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if snapshot.source_type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="This source has no managed connector configuration")
    return ConnectorConfigurationRead(
        workspace_id=snapshot.workspace_id, source_id=snapshot.source_id,
        source_type=snapshot.source_type,
        provider=snapshot.provider,
        source_generation=snapshot.source_generation,
        configuration=snapshot.configuration,
        expected_revision=snapshot.expected_revision,
        auth_method=snapshot.auth_method,
        auth_header_name=snapshot.auth_header_name,
        desired_enabled=snapshot.desired_enabled,
        activation_state=snapshot.activation_state,
        activation_error_code=snapshot.activation_error_code,
        provider_credential_configured=snapshot.provider_credential_configured,
        provider_credential_state=snapshot.provider_credential_state,
    )


@router.post("/{source_id}/validate-draft", response_model=DraftValidationRead)
async def validate_draft_configuration(
    source_id: UUID,
    payload: DraftValidationRequest,
    session: Session,
    request: Request,
    _owner: OwnerRead,
) -> DraftValidationRead:
    """Validate a draft and recheck its source, revision, and Telegram bot binding afterward.

    Retained-token reads lock source, provisioning, managed credentials, and the
    native row before decryption. Locks are released for provider requests, then
    reacquired to ensure the result still describes the requested active draft.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    if payload.expected_source_generation != source.generation:
        raise HTTPException(status_code=409, detail="Source generation changed; reload before validating")
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    current_revision = row.desired_revision if row is not None else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision changed; reload before validating")
    expected_auth = "telegram_bot_token" if source.provider == "telegram" else "none" if source.provider else None
    if expected_auth is not None and payload.auth_method != expected_auth:
        raise HTTPException(status_code=422, detail="Authentication mode does not match the provider")
    if source.provider is None and payload.auth_method == "telegram_bot_token":
        raise HTTPException(status_code=422, detail="Telegram authentication requires a Telegram source")
    if payload.auth_method == "http_header" and source.type != "api":
        raise HTTPException(status_code=422, detail="Header authentication is supported only for REST sources")
    try:
        source_configuration = serialize_source_configuration(source, payload.configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Draft provider configuration is invalid") from exc
    candidate = source.model_copy(update={"configuration": source_configuration})
    try:
        data = registry.validate(candidate)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Draft connector configuration is invalid") from exc
    checks: tuple[str, ...]
    bot_id: str | None = None
    scope_verified: bool | None = None
    if source.provider == "telegram":
        token: str
        try:
            previous = await provisioning.get_retained_native_credential_snapshot(
                session, source_id, source_generation=source.generation,
                connector_revision=payload.expected_revision,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Connector configuration changed; reload before validating") from exc
        validation_access_fence = previous.access_fence if previous is not None else access_fence
        if validation_access_fence != access_fence:
            raise HTTPException(status_code=409, detail="Original Telegram access changed")
        if payload.secret_action == "replace":
            token = payload.secret.get_secret_value() if payload.secret is not None else ""
        else:
            if previous is None or previous.state != "ready":
                raise HTTPException(status_code=409, detail="A validated Telegram token is required")
            key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
            try:
                token = decrypt_native_token(key, previous)
            except CredentialEncryptionUnavailable as exc:
                raise HTTPException(status_code=503, detail="Stored Telegram credential is unavailable") from exc
        await session.rollback()
        from modules.connectors.providers.telegram import validate_telegram_scope

        try:
            verified = await validate_telegram_scope(
                token, tuple(cast("list[str]", candidate.configuration["telegram_chat_ids"])),
                before_request=lambda: _validation_send_fence(
                    session, request, source, payload.expected_revision, scope=scope,
                    access_fence=validation_access_fence, native_snapshot=previous,
                ),
            )
        except ProviderRateLimited as exc:
            raise HTTPException(status_code=503, detail="Telegram provider rate limit reached") from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise HTTPException(status_code=503, detail="Telegram validation outcome is unknown") from exc
        except (ValueError, httpx.HTTPError) as exc:
            status = 503 if getattr(exc, "code", None) == "telegram_provider_unavailable" else 422
            detail = "Telegram validation outcome is unknown" if status == 503 else "Telegram bot identity and channel scope could not be verified"
            raise HTTPException(status_code=status, detail=detail) from exc
        await _validation_send_fence(
            session, request, source, payload.expected_revision, scope=scope,
            access_fence=validation_access_fence, native_snapshot=previous,
        )
        if previous is not None and previous.bound_bot_id not in (None, verified.verified_bot_id):
            raise HTTPException(status_code=409, detail="A different Telegram bot requires a new source")
        bot_id = verified.verified_bot_id
        scope_verified = True
        checks = ("configuration", "provider_identity", "provider_scope", "receive_mode")
    elif source.provider is not None:
        checks = ("configuration", "provider_identity", "provider_scope", "receive_mode")
        scope_verified = True
    else:
        if payload.auth_method == "http_header" and source.type != "api":
            raise HTTPException(status_code=422, detail="Header authentication is supported only for REST sources")
        try:
            await session.rollback()
            await validate_public_url(data["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Draft connector configuration is invalid") from exc
        checks = ("configuration", "public_url_policy")
    return DraftValidationRead(
        source_id=source_id,
        source_generation=source.generation,
        expected_revision=payload.expected_revision,
        validated_at=datetime.now(UTC),
        validation_status="valid",
        checks=checks,
        verified_bot_id=bot_id,
        scope_verified=scope_verified,
    )


async def _source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> ConnectorSource:
    """Read the explicit admitted workspace Source; foreign/missing IDs share HTTP 404.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.get("/catalog")
async def get_catalog(session: Session, request: Request, _owner: OwnerRead) -> list[catalog.CatalogEntry]:
    """Return connector catalog entries to an authorized owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    return list(catalog.list_catalog())


@router.put("/{source_id}/configuration", response_model=ActivationRead)
async def put_configuration(
    source_id: UUID,
    payload: ConnectorSettingsRequest,
    session: Session,
    request: Request, _owner: OwnerWrite,
) -> ActivationRead:
    """Save revision-checked desired settings without starting a new activation.

    Reconciles/cancels prior activation and workflow intent as needed, commits
    the saved configuration, and returns the resulting activation projection.
    Owner-write authorization is enforced by the route dependency.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source.status != "active" or source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    pending = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if pending is not None and pending.state == "disabled" and pending.error_code == "deactivation_pending":
        raise HTTPException(status_code=409, detail="Wait for source deactivation to finish before saving")
    expected_auth = "telegram_bot_token" if source.provider == "telegram" else "none" if source.provider else None
    if expected_auth is not None and payload.auth_method != expected_auth:
        raise HTTPException(status_code=422, detail="Authentication mode does not match the provider")
    if source.provider is None and payload.auth_method == "telegram_bot_token":
        raise HTTPException(status_code=422, detail="Telegram authentication requires a Telegram source")
    if payload.auth_method == "http_header" and source.type != "api":
        raise HTTPException(status_code=422, detail="Header authentication is supported only for REST sources")
    try:
        source_configuration = serialize_source_configuration(source, payload.configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Provider configuration is invalid") from exc
    desired_configuration = dict(source_configuration)
    desired_configuration["auth_method"] = payload.auth_method
    if payload.auth_header_name:
        desired_configuration["auth_header_name"] = payload.auth_header_name
    candidate = source.model_copy(update={"configuration": source_configuration})
    try:
        data = registry.validate(candidate)
        if "url" in data:
            await session.rollback()
            await validate_public_url(data["url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    saved_result = await connector_owner.save_connector_configuration(
        session,
        source,
        payload.expected_revision,
        source_configuration,
        desired_configuration,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if saved_result is None:
        raise HTTPException(status_code=409, detail="Source or connector revision changed while configuration was validated")
    saved, row = saved_result
    unresolved = await provisioning.unresolved_credential_error(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    row.state = "reconciliation_required" if unresolved else "saved_not_active"
    row.error_code = unresolved
    await commit_with_replay(session, [
        make_source_change(saved.id, saved.generation, saved.status, connector_state=row.state, scope=scope),
    ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return await _activation_read(session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


@router.post("/{source_id}/validate", response_model=ActivationRead)
async def validate_configuration(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ActivationRead:
    """Validate saved connector settings against configuration and URL policy.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    try:
        data = registry.validate(source)
        if "url" in data:
            await session.rollback()
            await validate_public_url(data["url"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Connector configuration is invalid") from exc
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if row is None:
        raise HTTPException(status_code=409, detail="Save connector configuration before validation")
    return await _activation_read(session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


@router.get("/{source_id}/activation", response_model=ActivationRead)
async def get_activation(
    source_id: UUID, session: Session, request: Request, _owner: OwnerRead
) -> ActivationRead:
    """Read activation status for an authorized source owner.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if row is None:
        return ActivationRead(
            workspace_id=scope.workspace_id, source_id=source_id,
            desired_revision=0,
            applied_revision=0,
            state="saved_not_active",
            error_code=None,
        )
    return await _activation_read(session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


@router.post("/{source_id}/activate", response_model=ActivationRead)
async def activate_source(
    source_id: UUID,
    payload: ActivationRequest,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ActivationRead:
    """Persist a revision-fenced activation bundle, then drive n8n operations.

    Owner-write authorization is enforced by the route dependency. The intent is
    committed before external calls. After dispatch, HTTP 503 can mean durable
    recovery work remains and a known n8n rejection maps to HTTP 422; validation
    failures can also return 422 before persistence. A non-2xx response after
    dispatch does not prove no external side effect occurred.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if row is None or row.desired_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if row.state == "provisioning":
        raise HTTPException(status_code=409, detail="Connector activation is already being reconciled")
    if row.state == "disabled" and row.error_code == "deactivation_pending":
        raise HTTPException(status_code=409, detail="Wait for source deactivation to finish before enabling")
    if await provisioning.unresolved_credential_error(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope):
        raise HTTPException(
            status_code=409,
            detail="An n8n credential operation is pending or requires recovery",
        )
    if source.status != "active" or source.generation != row.source_generation:
        raise HTTPException(status_code=409, detail="Source changed; save its current configuration before enabling")
    settings = request.app.state.settings
    api_key = settings.n8n_api_key.get_secret_value()
    if not api_key:
        if connector_owner.is_native_provider(source.provider):
            raise HTTPException(status_code=503, detail="Native activation contract is pending")
        raise HTTPException(status_code=503, detail="n8n provisioning is not configured")
    webhook_token = settings.n8n_webhook_token.get_secret_value()
    if not webhook_token:
        raise HTTPException(status_code=503, detail="Manual trigger authentication is not configured")
    encryption_key = settings.connector_credential_encryption_key.get_secret_value()
    try:
        secret_fingerprint(encryption_key, "connector-key-validation")
    except CredentialEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail="Connector credential encryption is not configured") from exc

    activation_id = uuid4()
    native_bot_id: str | None = None
    if source.provider == "telegram":
        from modules.connectors.providers.telegram import validate_telegram_scope

        try:
            previous = await provisioning.get_retained_native_credential_snapshot(
                session, source_id, source_generation=source.generation,
                connector_revision=payload.expected_revision,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Connector configuration changed; reload before enabling") from exc
        validation_access_fence = previous.access_fence if previous is not None else access_fence
        if validation_access_fence != access_fence:
            raise HTTPException(status_code=409, detail="Original Telegram access changed")
        if payload.secret_action == "keep":
            if previous is None or previous.state != "ready":
                raise HTTPException(status_code=409, detail="A validated Telegram token is required")
            try:
                native_token = decrypt_native_token(encryption_key, previous)
            except CredentialEncryptionUnavailable as exc:
                raise HTTPException(status_code=503, detail="Stored Telegram credential is unavailable") from exc
        else:
            native_token = payload.secret.get_secret_value() if payload.secret else ""
        await session.rollback()
        try:
            verified = await validate_telegram_scope(
                native_token, tuple(cast("list[str]", source.configuration.get("telegram_chat_ids", ()))),
                before_request=lambda: _validation_send_fence(
                    session, request, source, payload.expected_revision, scope=scope,
                    access_fence=validation_access_fence, native_snapshot=previous,
                ),
            )
        except ProviderRateLimited as exc:
            raise HTTPException(status_code=503, detail="Telegram provider rate limit reached") from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise HTTPException(status_code=503, detail="Telegram validation outcome is unknown") from exc
        except (ValueError, httpx.HTTPError) as exc:
            status = 503 if getattr(exc, "code", None) == "telegram_provider_unavailable" else 422
            detail = "Telegram validation outcome is unknown" if status == 503 else "Telegram bot identity and channel scope could not be verified"
            raise HTTPException(status_code=status, detail=detail) from exc
        if previous is not None and previous.bound_bot_id not in (None, verified.verified_bot_id):
            raise HTTPException(status_code=409, detail="A different Telegram bot requires a new source")
        await _validation_send_fence(
            session, request, source, payload.expected_revision, scope=scope,
            access_fence=validation_access_fence, native_snapshot=previous,
        )
        native_bot_id = verified.verified_bot_id
        ciphertext = encrypt_native_token(
            encryption_key, source_id=source_id, operation_id=activation_id,
            source_generation=source.generation, configuration_revision=payload.expected_revision,
            token=native_token, verified_bot_id=native_bot_id,
        )
        try:
            await provisioning.save_native_credential(
                session, source_id=source_id, operation_id=activation_id,
                source_generation=source.generation, connector_revision=payload.expected_revision,
                encrypted_token=ciphertext, token_fingerprint=secret_fingerprint(encryption_key, native_token),
                verified_bot_id=native_bot_id, validated_at=verified.validated_at,
                access_fence=validation_access_fence,
                expected_native_operation_id=previous.operation_id if previous is not None else None,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Telegram credential identity or revision changed") from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Telegram bot identity is already reserved") from exc
    elif payload.secret_action == "replace" and row.desired_configuration.get("auth_method") != "http_header":
        raise HTTPException(status_code=422, detail="This provider does not accept a replacement token")

    # Save/validation may have held native rows; restart from original admission
    # before staging all earlier bundle parents. Commit native save separately.
    if source.provider == "telegram":
        await commit_with_replay(session, [], scope=scope,
                                 multi_workspace_enabled=multi_workspace_enabled,
                                 access_fence=validation_access_fence)
    else:
        await session.rollback()
    access_fence = await _owner_access(session, request, scope, expected=access_fence)
    source_fence, row, slots = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
    )
    if (source_fence is None or row is None or source_fence.generation != source.generation
            or source_fence.status != "active" or row.desired_revision != payload.expected_revision):
        raise HTTPException(status_code=409, detail="Original activation configuration changed")
    try:
        credential_intents: dict[str, dict[str, object]] = {}
        required_credentials: dict[str, dict[str, object]] = {}

        collector = await provisioning.get_managed_credential(session, source_id, "collector", multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        collector_token: str | None = None
        collector_binding = collector.resolved_binding if collector is not None else None
        collector_scope = "mcp:collect" if source.type == "mcp" else "ingestion:write"
        if not (
            collector is not None and collector.state == "ready" and collector.credential_id
            and isinstance(collector_binding, dict)
            and collector_binding.get("source_generation") == source.generation
            and collector_binding.get("scope") == collector_scope
        ):
            collector_token = await ingestion.create_collector_credential_in_uow(
                session, source_id, credential_scope=collector_scope, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                source_fence=source_fence,
            )
            collector_binding = {
                "source_generation": source.generation,
                "scope": collector_scope,
                "token_fingerprint": secret_fingerprint(encryption_key, collector_token),
            }
        required, intent = prepare_credential_assignment(
            source_id=source_id,
            activation_id=activation_id,
            slot="collector",
            credential_name=f"BBD-OS source collector {source_id}",
            header_name="Authorization",
            secret=f"Bearer {collector_token}" if collector_token is not None else None,
            binding=collector_binding if isinstance(collector_binding, dict) else {},
            existing=collector,
            encryption_key=encryption_key,
        )
        required_credentials["collector"] = required
        if intent is not None:
            credential_intents["collector"] = intent

        manual = await provisioning.get_managed_credential(session, source_id, "manual_trigger", multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        required, intent = prepare_credential_assignment(
            source_id=source_id,
            activation_id=activation_id,
            slot="manual_trigger",
            credential_name="BBD-OS manual trigger",
            header_name="X-BBD-Webhook-Token",
            secret=webhook_token,
            binding={"binding_kind": "manual_trigger"},
            existing=manual,
            encryption_key=encryption_key,
        )
        required_credentials["manual_trigger"] = required
        if intent is not None:
            credential_intents["manual_trigger"] = intent

        if row.desired_configuration.get("auth_method") == "http_header":
            provider = await provisioning.get_managed_credential(session, source_id, "provider", multi_workspace_enabled=multi_workspace_enabled, scope=scope)
            provider_header = str(row.desired_configuration.get("auth_header_name", ""))
            if payload.secret_action == "keep":
                provider_binding = provider.resolved_binding if provider is not None else None
                if (
                    provider is None or provider.state != "ready" or not provider.credential_id
                    or not isinstance(provider_binding, dict)
                    or provider_binding.get("header_name") != provider_header
                ):
                    raise ValueError("Provide a provider credential for the current header binding")
                provider_secret = None
                binding = dict(provider_binding)
            else:
                provider_secret = payload.secret.get_secret_value() if payload.secret else None
                binding = {"binding_kind": "provider"}
            required, intent = prepare_credential_assignment(
                source_id=source_id,
                activation_id=activation_id,
                slot="provider",
                credential_name=f"BBD-OS REST source {source_id}",
                header_name=provider_header,
                secret=provider_secret,
                binding=binding,
                existing=provider,
                encryption_key=encryption_key,
            )
            required_credentials["provider"] = required
            if intent is not None:
                credential_intents["provider"] = intent
    except (CredentialEncryptionUnavailable, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not await provisioning.begin_activation_bundle_in_uow(
        session,
        source_id,
        source.generation,
        payload.expected_revision,
        dict(row.desired_configuration),
        activation_id,
        required_credentials,
        credential_intents,
     multi_workspace_enabled=multi_workspace_enabled, scope=scope, access_fence=access_fence):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Connector activation changed; reload and retry")
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, connector_state=row.state, scope=scope),
    ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)

    credentials = N8nCredentials(str(settings.n8n_service_url), api_key)
    api = N8nApi(str(settings.n8n_service_url), api_key)
    await drive_activation(
        session,
        source_id,
        api,
        credentials,
        encryption_key,
     multi_workspace_enabled=multi_workspace_enabled, scope=scope, access_fence=access_fence)
    latest = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if latest is None:
        raise HTTPException(status_code=404, detail="Connector state disappeared during activation")
    if latest.state != "active":
        if latest.state == "saved_not_active" and latest.error_code == "n8n_credential_rejected":
            raise HTTPException(status_code=422, detail="n8n rejected a connector credential; review it and retry")
        raise HTTPException(status_code=503, detail="Connector activation is pending reconciliation")
    return await _activation_read(session, source_id, latest, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


@router.post("/{source_id}/deactivate", response_model=ActivationRead)
async def deactivate_source(
    source_id: UUID,
    session: Session,
    request: Request, _owner: OwnerWrite,
) -> ActivationRead:
    """Pause connector collection and persist the resulting activation state.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await sources.pause_source_for_connector(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if row is None:
        raise HTTPException(status_code=409, detail="No connector provisioning state exists")
    await commit_with_replay(session, [
        make_source_change(source.id, source.generation, source.status, connector_state=row.state, scope=scope),
    ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return await _activation_read(session, source_id, row, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


@router.delete("/{source_id}/credentials/provider", response_model=ActivationRead)
async def remove_provider_credential(
    source_id: UUID,
    expected_revision: Annotated[int, Query(ge=1)],
    session: Session,
    request: Request, _owner: OwnerWrite,
) -> ActivationRead:
    """Disable provider authentication and queue a fenced credential deletion.

    Owner-write authorization is enforced by the route dependency. The local
    configuration and delete intent commit before reconciliation; the external
    n8n credential remains until the later delete acknowledgement clears it.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    row = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None or row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    if row.desired_revision != expected_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision is stale")
    if (
        source.status != "paused"
        or row.state != "disabled"
        or row.error_code == "deactivation_pending"
    ):
        raise HTTPException(status_code=409, detail="Pause the source before removing its provider credential")
    revocation_snapshot = None
    if source.provider == "telegram":
        revocation_snapshot = await provisioning.get_native_credential_revocation_snapshot(
            session, source_id, source_generation=source.generation,
            connector_revision=row.desired_revision, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        revocation_access_fence = revocation_snapshot.access_fence if revocation_snapshot is not None else access_fence
        if revocation_access_fence != access_fence:
            raise HTTPException(status_code=409, detail="Original credential revocation access changed")
        await session.rollback()
        await _owner_access(session, request, scope, expected=revocation_access_fence)
    desired = dict(row.desired_configuration)
    desired["auth_method"] = "none"
    desired.pop("auth_header_name", None)
    saved = await connector_owner.save_connector_configuration(
        session,
        source,
        expected_revision,
        dict(source.configuration),
        desired,
        allow_paused=True,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if saved is None:
        raise HTTPException(status_code=409, detail="Connector configuration revision changed")
    saved_source, updated = saved
    updated.state = "disabled"
    if source.provider == "telegram":
        try:
            await provisioning.revoke_native_credential_in_uow(
                session, source_id, source_generation=saved_source.generation,
                connector_revision=updated.desired_revision, release_bot_reservation=True,
                access_fence=revocation_access_fence,
                expected_native_operation_id=revocation_snapshot.operation_id if revocation_snapshot is not None else None,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Telegram credential revision changed") from exc
        await commit_with_replay(session, [
            make_source_change(saved_source.id, saved_source.generation, saved_source.status, connector_state=updated.state, scope=scope),
        ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        return await _activation_read(session, source_id, updated, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    intent = await provisioning.create_delete_intent_in_uow(
        session, source_id, "provider", updated.desired_revision, access_fence=access_fence,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if intent is None:
        existing = await provisioning.get_managed_credential(session, source_id, "provider", multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        if existing is not None and existing.credential_id is not None:
            raise HTTPException(status_code=409, detail="Provider credential operation cannot be changed until its current operation settles")
        updated.error_code = None
    else:
        updated, _, _ = intent
        updated.error_code = "credential_delete_pending"
    await commit_with_replay(session, [
        make_source_change(saved_source.id, saved_source.generation, saved_source.status, connector_state=updated.state, scope=scope),
    ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return await _activation_read(session, source_id, updated, multi_workspace_enabled=multi_workspace_enabled, scope=scope)


async def _activation_read(
    session: AsyncSession, source_id: UUID, row: ConnectorProvisioning, *, scope: Scope, multi_workspace_enabled: bool
) -> ActivationRead:
    """Project durable activation state and unresolved credential errors.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    state = row.state
    error_code = row.error_code
    unresolved = await provisioning.unresolved_credential_error(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if unresolved:
        error_code = unresolved
        if state != "disabled":
            state = "reconciliation_required"
    return ActivationRead(
        workspace_id=scope.workspace_id, source_id=source_id,
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        state=state,
        error_code=error_code,
        credential_recovery=(
            "unsupported_operation" if unresolved else "supported"
        ),
    )
