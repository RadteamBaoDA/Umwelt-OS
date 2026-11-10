"""Shared router, dependency aliases, request/response schemas and owner-fence helpers for the connector provisioning routes."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence
from core.workspaces.schemas import AccessFence, Scope, WorkspaceContext
from modules.connectors import (
    provisioning,
)
from modules.connectors.models import (
    ConnectorNativeCredential,
    ConnectorProvisioning,
)
from modules.connectors.public import (
    ConnectorConfig,
    NativeCredentialSnapshot,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _invalid_detail(prefix: str, exc: Exception) -> str:
    """Return the 422 detail for a rejected connector configuration.

    Registry and serializer checks raise plain ``ValueError`` with fixed, value-free sentences (for example the
    missing field names), so that sentence is appended to tell the owner what to fix. Pydantic ``ValidationError``
    and other subclasses can echo submitted values, so only the fixed prefix is returned for them.
    """
    return f"{prefix}: {exc}" if type(exc) is ValueError else prefix


async def _owner_access(
    session: AsyncSession,
    request: Request,
    scope: WorkspaceContext,
    *,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Reject members and lock original account/session/workspace before domain access.

    Existing write dependencies retain CSRF admission. Capture this fence once;
    later publication and provider callbacks compare it rather than renewing an epoch.
    Caller releases SQL before network and owns final commit/rollback.
    """
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await lock_access_fence(
        session,
        scope=scope,
        expected=expected,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),),
    )


async def _validation_send_fence(
    session: AsyncSession,
    request: Request,
    source: ConnectorSource,
    revision: int,
    *,
    scope: WorkspaceContext,
    access_fence: AccessFence,
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
            session,
            source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=scope,
            multi_workspace_enabled=gate,
            expected_access_fence=access_fence,
        )
        if (
            current_source is None
            or current_source.status != "active"
            or current_source.workspace_id != source.workspace_id
            or current_source.generation != source.generation
            or (row.desired_revision if row is not None else 0) != revision
        ):
            raise HTTPException(
                status_code=409, detail="Original Telegram validation scope changed"
            )
        native = await session.scalar(
            select(ConnectorNativeCredential)
            .where(ConnectorNativeCredential.source_id == source.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if native_snapshot is None:
            matches = native is None
        else:
            matches = (
                native is not None
                and native_snapshot.access_fence == access_fence
                and all(
                    getattr(native, field) == getattr(native_snapshot, field)
                    for field in (
                        "source_id",
                        "operation_id",
                        "source_generation",
                        "configuration_revision",
                        "verified_bot_id",
                        "bound_bot_id",
                        "encrypted_token",
                        "state",
                        "validated_at",
                    )
                )
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
    secret_action: Literal["keep", "replace"] = "keep"  # noqa: S105  # label/URL/enum value, not a credential
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
    secret_action: Literal["keep", "replace"] = "keep"  # noqa: S105  # label/URL/enum value, not a credential
    secret: SecretStr | None = None
    # Omitted: preserve an existing source's backend; a never-activated supported source defaults to native.
    execution_backend: Literal["native", "n8n"] | None = None

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
    execution_backend: Literal["native", "n8n"] = "n8n"
    transition_phase: Literal[
        "idle", "draining", "deactivating_old", "activating_new", "reconciliation_required"
    ] = "idle"
    target_backend: Literal["native", "n8n"] | None = None
    backend_revision: int = 1


class BackendTransitionRequest(BaseModel):
    """Request a revision-fenced backend switch, or an n8n template upgrade when the target is unchanged."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    target_backend: Literal["native", "n8n"]


class BackendResolveRequest(BaseModel):
    """Leave reconciliation_required: retry the old-workflow stop or confirm it inactive out of band."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    action: Literal["retry", "confirm_inactive"]


class NativeRestCredentialRequest(BaseModel):
    """Owner re-entry of the REST header secret for the native backend; write-only, never echoed."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    secret: SecretStr

    @model_validator(mode="after")
    def validate_secret(self) -> "NativeRestCredentialRequest":
        """Apply the same bounded-syntax rule as activation replacement secrets."""
        _validate_secret_action("replace", self.secret)
        return self


class ConnectorConfigurationRead(BaseModel):
    """Expose source-scoped connector settings without returning secret values."""

    workspace_id: UUID
    source_id: UUID
    source_type: str
    source_generation: int
    configuration: dict[str, object]
    provider: str | None = None
    expected_revision: int
    auth_method: Literal["none", "http_header", "telegram_bot_token", "oauth2"]
    auth_header_name: str | None
    desired_enabled: bool
    activation_state: str
    activation_error_code: str | None
    provider_credential_configured: bool
    provider_credential_state: str | None
    execution_backend: Literal["native", "n8n"] = "n8n"
    transition_phase: str = "idle"


class DraftValidationRead(BaseModel):
    """Report successful draft validation against a source generation."""

    source_id: UUID
    source_generation: int
    expected_revision: int
    validated_at: datetime
    validation_status: Literal["valid"]
    checks: tuple[
        Literal[
            "configuration",
            "public_url_policy",
            "provider_identity",
            "provider_scope",
            "receive_mode",
        ],
        ...,
    ]
    verified_bot_id: str | None = Field(default=None, max_length=20)
    scope_verified: bool | None = None


class RestSampleRead(BaseModel):
    """Bounded JSON sample from a custom REST draft: status and pruned body only, never headers or URL."""

    status_code: int
    truncated: bool
    sample: Any


async def _source(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool
) -> ConnectorSource:
    """Read the explicit admitted workspace Source; foreign/missing IDs share HTTP 404.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    source = await sources.get_connector_source(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


async def _activation_read(
    session: AsyncSession,
    source_id: UUID,
    row: ConnectorProvisioning,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ActivationRead:
    """Project durable activation state and unresolved credential errors.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    state = row.state
    error_code = row.error_code
    unresolved = await provisioning.unresolved_credential_error(
        session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope
    )
    if unresolved:
        error_code = unresolved
        if state != "disabled":
            state = "reconciliation_required"
    return ActivationRead(
        workspace_id=scope.workspace_id,
        source_id=source_id,
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        state=state,
        error_code=error_code,
        credential_recovery=("unsupported_operation" if unresolved else "supported"),
        execution_backend=row.execution_backend,
        transition_phase=row.transition_phase,
        target_backend=row.target_backend,
        backend_revision=row.backend_revision,
    )


# --------------------------------------------------------------------------- C4 native backend
