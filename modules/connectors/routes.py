from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator, require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.errors import DomainError
from core.ids import uuid7
from core.rate_limit import require_budget
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import list_instance_workspaces, lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.connectors import mcp as mcp_collection
from modules.connectors import provider_terms, provisioning, registry, scheduler
from modules.connectors import public as connectors_public
from modules.connectors.collection_schemas import (
    CollectionAdmissionRead,
    CollectionAdmissionRequest,
    CollectionRequestRead,
    CollectionRequestRef,
    ManagedConnectorReceipt,
    ManagedCrawlRequest,
    ManagedNoChanges,
)
from modules.connectors.models import (
    ConnectorCollectionRequest,
    ConnectorProvisioning,
    ConnectorWorldCredential,
)
from modules.connectors.native_fetch import (
    ProviderFetchRead,
    ProviderFetchRequest,
    run_provider_fetch,
)
from modules.connectors.native_fetch import (
    settle_request_exit as _settle_request_exit,
)
from modules.connectors.provider_specs import (
    COLLECT_COOLDOWN_FLOOR_MINUTES,
    TERMS_INELIGIBLE,
    collection_cooldown_seconds,
    get_provider_spec,
    min_interval_minutes,
)
from modules.connectors.public import (
    AgentBrowserGrantPatch,
    CollectionFence,
    ConnectorConfigurationRequest,
    ConnectorPreview,
    CrawlResult,
    RSSRequest,
    is_native_provider,
    resolve_agent_browser_scope,
    save_connector_configuration,
    serialize_source_configuration,
    update_agent_browser_grant_in_uow,
    validate_public_url,
    wake_packaged_collection,
)
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import (
    Receipt,
    ReceiveBatch,
)
from modules.settings.public import module_dependency, module_is_enabled, n8n_enabled, resolve_n8n
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource

router = APIRouter(prefix="/api/v1/connectors/sources", tags=["connectors"])
Session = Annotated[AsyncSession, Depends(get_session)]
operator_router = APIRouter(prefix="/api/v1/operator/connectors", tags=["connectors"])
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@operator_router.get("/terms-review", response_model=list[provider_terms.OperatorTermsQueueItem])
async def list_operator_terms_review(
    session: Session,
    operator: Annotated[AuthSession, Depends(require_owner)],
) -> list[provider_terms.OperatorTermsQueueItem]:
    """List pending provider terms across workspaces without exposing evidence or credentials."""
    del operator
    workspaces = await list_instance_workspaces(session)
    names = {workspace.id: workspace.name for workspace in workspaces}
    return await provider_terms.list_operator_pending_terms(session, names)


async def _owner_access(session: AsyncSession, request: Request, scope: WorkspaceContext, *, expected: AccessFence | None = None) -> AccessFence:
    """Reject members and lock original account/session/workspace before domain access.

    Existing write dependencies retain CSRF admission. Capture this fence once;
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


class ConnectorState(BaseModel):
    """Represent a connector's source, configuration, cursor, and revision state."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    source_id: UUID
    source_generation: int
    connector_revision: int | None = None
    type: str
    timezone: str
    url: str
    configuration: dict[str, Any]
    cursor_before: str | None
    catch_up_since: str | None


class ManualSyncResult(BaseModel):
    """Return identifiers and current status for manually requested collection."""
    run_id: UUID | None = None
    batch_id: UUID | None = None
    status: str = "queued"
    failed_calls: list[str] = []
    request_id: UUID | None = None  # native backend: the durable collection request to poll


class McpCollectionRequest(BaseModel):
    """Carry a generation-checked MCP collection configuration; it never carries commands or URLs."""
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=1)
    configuration: mcp_collection.McpCollectionConfig


class McpScheduledCollectionRequest(BaseModel):
    """Carry only the source, provisioning, and reviewed connection identities baked into n8n."""
    model_config = ConfigDict(extra="forbid")

    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int | None = Field(default=None, ge=1)
    connection_id: UUID
    admission_request_id: UUID
    admission_token: UUID


class WorldProviderCredentialPut(BaseModel):
    """Carry one write-only provider API key under source and connector revision fences."""
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    expected_connector_revision: int = Field(ge=1)
    api_key: str = Field(min_length=1, max_length=256)


@router.put(
    "/{source_id}/world-data-credential",
    dependencies=[Depends(module_dependency("connectors"))],
)
async def save_world_provider_credential(
    source_id: UUID, payload: WorldProviderCredentialPut, request: Request,
    session: Session, _owner: OwnerWrite,
) -> dict[str, object]:
    """Encrypt and replace the Alpha Vantage key without returning its value.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    source = await sources.lock_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    # PRODUCTION FIX: SourceFence has no provider; read it from the connector projection (row is locked above).
    connector = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if connector is None or connector.provider != "alpha_vantage" or source.status != "active" or source.generation != payload.expected_generation:
        raise HTTPException(status_code=409, detail="Alpha Vantage source generation changed")
    provisioning_row = await session.get(ConnectorProvisioning, source_id, with_for_update=True)
    if provisioning_row is None or provisioning_row.desired_revision != payload.expected_connector_revision:
        raise HTTPException(status_code=409, detail="Connector configuration revision changed")
    key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
    if not key:
        raise HTTPException(status_code=503, detail="Provider credential encryption is unavailable")
    from modules.connectors.credentials import encrypt_credential_input, secret_fingerprint

    operation_id = uuid7()
    ciphertext = encrypt_credential_input(
        key, source_id=source.id, slot="native:alpha_vantage", operation_id=operation_id,
        request={"api_key": payload.api_key},
        binding={"provider": "alpha_vantage", "source_generation": source.generation,
                 "configuration_revision": provisioning_row.desired_revision,
                 "fingerprint": secret_fingerprint(key, payload.api_key)},
    )
    credential = await session.get(ConnectorWorldCredential, source_id, with_for_update=True)
    values = {
        "provider": "alpha_vantage", "source_generation": source.generation,
        "configuration_revision": provisioning_row.desired_revision,
        "operation_id": operation_id, "encrypted_key": ciphertext,
    }
    if credential is None:
        session.add(ConnectorWorldCredential(source_id=source.id, **values))
    else:
        for name, value in values.items():
            setattr(credential, name, value)
    await session.commit()
    return {"source_id": source.id, "configured": True, "provider": "alpha_vantage"}


@router.put("/{source_id}/mcp-collection", dependencies=[Depends(module_dependency("connectors"))])
async def configure_mcp_collection(
    source_id: UUID, payload: McpCollectionRequest, session: Session, request: Request, _owner: OwnerWrite
) -> dict[str, Any]:
    """Save the allowlisted connection and grant calls of an active ``mcp`` source.

    Grants are reviewed on the MCP connection (purpose collection, scoped to this source);
    this route only pins which reviewed grants run with which fixed arguments. Semantic
    validity is enforced at collection time against live fences.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source.type != mcp_collection.PROVIDER_ID or source.status != "active":
        raise HTTPException(status_code=409, detail="Active MCP source required")
    saved = await sources.set_connector_configuration(
        session, source_id, payload.expected_generation,
        payload.configuration.model_dump(mode="json"),
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if saved is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Source changed while configuration was validated")
    prior = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    provisioned = await provisioning._save_desired_in_uow(
        session, source_id, saved.generation,
        prior.desired_revision if prior is not None else 0,
        saved.configuration,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if provisioned is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="MCP source changed while scheduled configuration was saved")
    await commit_with_replay(session, [make_source_change(
        saved.id, saved.generation, saved.status, connector_state=provisioned.state,
     scope=scope)], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return {
        "source_id": saved.id, "source_generation": saved.generation,
        "expected_revision": provisioned.desired_revision,
        "configuration": saved.configuration,
    }


@router.post("/{source_id}/mcp-collect", response_model=ManualSyncResult, status_code=202)
async def collect_mcp_scheduled(
    source_id: UUID, payload: McpScheduledCollectionRequest, session: Session,
    request: Request, authorization: Annotated[str | None, Header()] = None,
) -> ManualSyncResult:
    """Run n8n MCP collection under its bearer, source, connector revision and grant fences.

    The same source generation, applied connector revision and reviewed connection identity are
    carried into the collector so each provider request and final receipt can recheck them.

    The admitted request is proved, then settled by the collector's single ingestion proof
    (receipt or no-change); a collector error settles it failed (401 when the bearer was
    revoked, otherwise 409) so the slot frees immediately.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    token, scope, _access_fence = await _mcp_collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source.type != mcp_collection.PROVIDER_ID or source.status != "active":
        raise HTTPException(status_code=409, detail="Active MCP source required")
    try:
        config = mcp_collection.validate(source)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="MCP source configuration is invalid") from exc
    if config.connection_id != payload.connection_id or not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True, backend_revision=payload.backend_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="MCP source or provisioning revision is stale")
    ref = CollectionRequestRef(request_id=payload.admission_request_id, admission_token=payload.admission_token)
    await _prove_admission(
        session, ref, source_id=source_id, source_generation=payload.source_generation,
        connector_revision=payload.connector_revision, scope=scope)
    runtime = getattr(request.app.state, "mcp_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="MCP runtime is unavailable")
    # Release the access-fence/Source locks: no lock may be held across MCP I/O.
    await session.rollback()
    try:
        result = await mcp_collection.collect(
            runtime, request.app.state.session_factory, source_id, payload.connector_revision,
            payload.source_generation, collector_token=token, expected_connection_id=payload.connection_id,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, request_ref=ref,
        )
    except mcp_collection.McpCollectionError as exc:
        # Free the admitted slot now; the sweeper would otherwise hold it until expiry.
        await _settle_request_exit(session, ref, outcome="failed", error_code=exc.code)
        raise HTTPException(
            status_code=401 if exc.code == "mcp_collector_revoked" else 409, detail=exc.code) from exc
    except LookupError as exc:
        await _settle_request_exit(session, ref, outcome="failed", error_code="source_not_found")
        raise HTTPException(status_code=404, detail="Source not found") from exc
    return ManualSyncResult(
        run_id=result["run_id"], batch_id=result["batch_id"], status=result["status"],
        failed_calls=result["failed_calls"])


@router.get("/{source_id}/agent-browser-grant", dependencies=[Depends(module_dependency("connectors"))])
async def read_agent_browser_grant(
    source_id: UUID, session: Session, request: Request, _owner: OwnerWrite
) -> dict[str, object]:
    """Return the current source-bound browser opt-in, without exposing connector credentials.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    grant = await resolve_agent_browser_scope(session, _owner.user_id, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if grant is None:
        return {"available": False, "enabled": False}
    return {"available": True, **grant.__dict__}


@router.put("/{source_id}/agent-browser-grant", dependencies=[Depends(module_dependency("connectors"))])
async def update_agent_browser_grant(
    source_id: UUID,
    payload: AgentBrowserGrantPatch,
    expected_revision: int,
    session: Session,
    request: Request, _owner: OwnerWrite,
) -> dict[str, object]:
    """Apply owner browser opt-in with optimistic grant and source configuration fences.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    await _owner_access(session, request, scope)
    try:
        grant = await update_agent_browser_grant_in_uow(
            session, _owner.user_id, source_id, expected_revision, payload,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        await session.commit()
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Active web source is unavailable") from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Browser grant revision is stale") from exc
    return {"available": True, **grant.__dict__}


async def _prove_admission(
    session: AsyncSession, ref: CollectionRequestRef, *, source_id: UUID, source_generation: int,
    connector_revision: int, scope: Scope,
) -> None:
    """Re-prove the running request, unexpired slot and token under lock; stale or replayed is 409.

    Flush-only: the caller already holds the access fence and Source lock, and releases them with
    its own rollback or commit. Used by fetch-only routes, which authorize a fetch but never settle.
    """
    if not await connectors_public.lock_collection_request_in_uow(
        session, ref, source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Collection admission is no longer current")


async def _source(session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool) -> ConnectorSource:
    """Read only the explicit owner/job Source projection; absent/foreign identity is 404.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    source = await sources.get_connector_source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


async def _collector(
    session: AsyncSession, source_id: UUID, authorization: str | None, *,
    multi_workspace_enabled: bool, credential_scope: str = "ingestion:write",
) -> tuple[str, InternalJobScope, AccessFence]:
    """Resolve the exact bearer/path/capability through Ingestion before domain locks.

    Only this entrypoint derives an internal principal. Subsequent checks retain that
    subject and its original AccessFence; a regrant cannot renew a running request.
    The actual rollout flag and scoped module gates remain required. A disabled n8n (owner kill
    switch) rejects with 409 ``n8n_disabled`` via ``require_n8n_backend``.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Source collector authentication required")
    scope = await ingestion.resolve_collector_job_scope(
        session, token, source_id=source_id, credential_scope=credential_scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if scope is None:
        raise HTTPException(status_code=401, detail="Source collector authentication required")
    access_fence = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    await _collector_current(
        session, source_id, token, credential_scope=credential_scope, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    source_fence = await sources.lock_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        expected_access_fence=access_fence,
    )
    if source_fence is None:
        raise HTTPException(status_code=401, detail="Source collector authentication required")
    # Bearer routes belong to the packaged n8n workflow: a native or transitioning source rejects them.
    # Resolve the owner kill switch once per collector call (workspace known only after the bearer resolves).
    n8n_on = await n8n_enabled(session, workspace_id=scope.workspace_id)
    await provisioning.require_n8n_backend(session, source_id, n8n_enabled=n8n_on)
    return token, scope, access_fence


async def _collector_current(
    session: AsyncSession, source_id: UUID, token: str, *, scope: InternalJobScope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
    credential_scope: str = "ingestion:write",
) -> None:
    """Recheck the original collector epoch without acquiring earlier locks or resolving anew.

    This authenticates a literal capability only; durable native lease/request admission
    remains with Ingestion. Missing token/module or changed original fence fails closed.
    """
    current = await read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if current != access_fence:
        raise HTTPException(status_code=409, detail="Original collector access changed")
    if not await ingestion.collector_can_ingest(
        session, source_id, token, credential_scope=credential_scope,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise HTTPException(status_code=401, detail="Source collector authentication required")
    for module_id in (("connectors", "tools") if credential_scope == "mcp:collect" else ("connectors",)):
        if not await module_is_enabled(
            session, module_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        ):
            raise HTTPException(status_code=404, detail="Connector collection unavailable")


async def _mcp_collector(
    session: AsyncSession, source_id: UUID, authorization: str | None, *,
    multi_workspace_enabled: bool,
) -> tuple[str, InternalJobScope, AccessFence]:
    """Resolve only the distinct mcp:collect bearer into its exact original service subject."""
    return await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
        credential_scope="mcp:collect",
    )


@router.post("/{source_id}/provider-fetch", response_model=ProviderFetchRead)
async def fetch_native_provider(
    source_id: UUID,
    payload: ProviderFetchRequest,
    session: Session,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ProviderFetchRead:
    """Collect one bounded native page under bearer, source, revision, and durable lease fences.

    This route only authenticates the collector bearer, loads the Source and checks the
    Ingestion module gate (404 when disabled); orchestration, provider selection, cooldowns,
    leases and error translation live in ``native_fetch.run_provider_fetch`` and the
    ``ProviderCollector`` registry. Shared provider cooldowns return before reserving a lease;
    a provider retry deadline releases the reservation and returns rate_limited without a
    receipt, cursor or success health. GitHub's single 60-second segment deadline covers
    cooldown, admission, reservation, one selected GET and receipt acceptance.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    request_ref = CollectionRequestRef(request_id=payload.admission_request_id, admission_token=payload.admission_token)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if not await module_is_enabled(session, "ingestion", multi_workspace_enabled=multi_workspace_enabled, scope=scope):
        raise HTTPException(status_code=404, detail="Provider collection is unavailable")
    return await run_provider_fetch(
        session, request, source, payload, collector_token=collector_token,
        request_ref=request_ref, scope=scope, access_fence=access_fence,
    )


@router.put("/{source_id}/configuration", response_model=ConnectorState,
            dependencies=[Depends(module_dependency("connectors"))])
async def configure_source(
    source_id: UUID, payload: ConnectorConfigurationRequest, session: Session, request: Request, _owner: OwnerWrite
) -> ConnectorState:
    """Validate and persist generic RSS/Web/REST settings; named native sources use revisioned provider settings.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Use provider settings to configure a native source")
    if source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=422, detail="This source type has no packaged connector")
    try:
        source_configuration = serialize_source_configuration(source, payload.configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Provider configuration is invalid") from exc
    candidate = source.model_copy(update={"configuration": source_configuration})
    try:
        data = registry.validate(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await session.rollback()
        await validate_public_url(data["url"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    desired = dict(candidate.configuration)
    desired["auth_method"] = "none"
    await _owner_access(session, request, scope, expected=access_fence)
    saved_result = await save_connector_configuration(
        session,
        source,
        payload.expected_revision,
        candidate.configuration,
        desired,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if saved_result is None:
        raise HTTPException(status_code=409, detail="Source or connector revision changed while configuration was validated")
    saved, provisioning_row = saved_result
    result = registry.sync(saved, None)
    result["workspace_id"] = saved.workspace_id
    result["connector_revision"] = provisioning_row.desired_revision
    await commit_with_replay(session, [
        make_source_change(
            saved.id, saved.generation, saved.status,
            connector_state=provisioning_row.state,
         scope=scope),
    ], access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    return ConnectorState(**result)


@router.get("/{source_id}/terms", response_model=provider_terms.ProviderTermsRead | None,
            dependencies=[Depends(module_dependency("connectors"))])
async def read_provider_terms(
    source_id: UUID, session: Session, request: Request, _owner: Annotated[WorkspaceContext, Depends(require_workspace_read)],
) -> provider_terms.ProviderTermsRead | None:
    """Owner read of provider terms state; members have no Source access."""
    if _owner.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await provider_terms.get_terms(
        session, source_id, scope=_owner, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.put("/{source_id}/terms", response_model=provider_terms.ProviderTermsRead,
            dependencies=[Depends(module_dependency("connectors"))])
async def acknowledge_provider_terms(
    source_id: UUID, payload: provider_terms.ProviderTermsAcknowledge, session: Session, request: Request, _owner: OwnerWrite,
) -> provider_terms.ProviderTermsRead:
    """Owner acknowledges provider terms; clears a terms action-required gate at the new revision.

    Review-class providers also need operator review evidence. The single-user instance operator is the
    reviewer, so their own acknowledgement records it here, exactly as the built-in toggle does; any other
    owner still waits for the operator's review queue. Ineligible declared uses record nothing.
    """
    await _owner_access(session, request, _owner)
    # Read the auth-session ORM row before rollback expires it (lazy refresh is illegal under asyncio).
    is_operator = is_instance_operator(request.state.auth_session)
    reviewer_user_id = request.state.auth_session.owner_id
    await session.rollback()
    result = await provider_terms.acknowledge_terms(
        session, source_id, payload, scope=_owner,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    spec = get_provider_spec(result.provider_id)
    if (spec is not None and spec.eligibility == "review" and result.decision == "operator_review_required"
            and result.declared_use in ("personal", "noncommercial", "commercial")
            and is_operator):
        result = await provider_terms.record_operator_review(
            session, workspace_id=_owner.workspace_id, source_id=source_id,
            reviewer_user_id=reviewer_user_id,
            body=provider_terms.OperatorTermsReview(
                state="approved", allowed_use=cast(Any, result.declared_use),
                evidence_ref=f"operator-acknowledge:{spec.id}:{spec.terms_url}@{spec.checked_on}",
                reviewed_terms_version=result.terms_version),
            instance_operator=True)
    await scheduler.clear_collection_block(session, source_id, terms_revision=result.terms_revision)
    await session.commit()
    return result


@operator_router.put("/{workspace_id}/{source_id}/terms-review", response_model=provider_terms.ProviderTermsRead)
async def review_provider_terms(
    workspace_id: UUID, source_id: UUID, payload: provider_terms.OperatorTermsReview, session: Session,
    operator: Annotated[AuthSession, Depends(require_owner_write)],
) -> provider_terms.ProviderTermsRead:
    """Bootstrap-operator review of a review-class provider's acknowledged terms (no workspace role grants it)."""
    if not is_instance_operator(operator):
        raise HTTPException(status_code=403, detail="Instance operator required")
    await session.rollback()
    result = await provider_terms.record_operator_review(
        session, workspace_id=workspace_id, source_id=source_id, reviewer_user_id=operator.owner_id,
        body=payload, instance_operator=True)
    await scheduler.clear_collection_block(
        session, source_id, terms_revision=result.terms_revision, workspace_id=workspace_id)
    await session.commit()
    return result


async def _precheck_mcp_enabled(
    session: AsyncSession, source_id: UUID, source: ConnectorSource,
    *, multi_workspace_enabled: bool, scope: WorkspaceContext,
) -> None:
    """Require an enabled, fully applied MCP source before a manual collection (HTTP 409 otherwise).

    Reads activation state and takes the collection fence with ``lock=True`` (the caller's
    later rollback/commit releases it). No commit here.
    """
    provisioned = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if (
        source.status != "active" or provisioned is None
        or provisioned.state != "active" or not provisioned.desired_enabled
        or provisioned.applied_revision != provisioned.desired_revision
        or provisioned.source_generation != source.generation
        or not await provisioning.require_collection_fence(
            session, source, source.generation, provisioned.desired_revision, lock=True,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
    ):
        raise HTTPException(status_code=409, detail="Enable this MCP source before collecting")


async def _queue_native_collection(
    session: AsyncSession, request: Request, source_id: UUID, source: ConnectorSource, revision: int,
    *, scope: WorkspaceContext, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> ManualSyncResult:
    """Apply the slow-provider cooldown, then queue a durable manual collection request.

    Slow/unofficial providers (floor >= 6 h) get a cooldown equal to their schedule floor
    (429 + ``Retry-After``); the lookup is skipped for fast providers. Rolls back, re-admits with
    the original fence, then ``scheduler.request_collection`` (terms-ineligible maps to 409
    ``terms_not_accepted``).
    """
    last_started = None
    if min_interval_minutes(source.provider) >= COLLECT_COOLDOWN_FLOOR_MINUTES:
        last_started = await session.scalar(select(func.max(ConnectorCollectionRequest.attempt_started_at)).where(
            ConnectorCollectionRequest.workspace_id == scope.workspace_id,
            ConnectorCollectionRequest.source_id == source_id,
            ConnectorCollectionRequest.status.in_(("running", "succeeded", "no_changes"))))
    wait = collection_cooldown_seconds(source.provider, last_started, datetime.now(UTC))
    if wait:
        raise HTTPException(
            status_code=429, detail={"code": "collection_too_soon", "retry_after": wait},
            headers={"Retry-After": str(wait)})
    await session.rollback()
    await _owner_access(session, request, scope, expected=access_fence)
    try:
        queued = await scheduler.request_collection(
            session, scope, source_id, "manual", revision, multi_workspace_enabled=multi_workspace_enabled)
    except (HTTPException, DomainError) as exc:
        if exc.detail == TERMS_INELIGIBLE:
            raise HTTPException(status_code=409, detail="terms_not_accepted") from exc
        raise
    return ManualSyncResult(status=queued.status, request_id=queued.request_id)


@router.post("/{source_id}/collect", response_model=ManualSyncResult, status_code=202,
             dependencies=[Depends(module_dependency("connectors")), Depends(require_budget("connector_run", 10, 60))])
async def trigger_collection(
    source_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ManualSyncResult:
    """Request collection in the selected owner workspace under its captured applied revision.

    Packaged n8n wake uses explicit original scope. MCP sources, once prechecked as enabled,
    follow the generic flow: the native backend queues a durable request run by the worker
    (the saved allowlisted calls only; no request input names a tool, URL or argument), and the
    n8n backend wakes the packaged workflow, which settles via ``/mcp-collect``.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    settings = request.app.state.settings
    if source.type == mcp_collection.PROVIDER_ID:
        await _precheck_mcp_enabled(
            session, source_id, source, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        # Precheck passed: fall through to the generic native/n8n dispatch below.
    if source.status != "active" or source.type not in {"rss", "web", "api", mcp_collection.PROVIDER_ID}:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    provisioned = await provisioning.activation_status(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if (provisioned is None or provisioned.state != "active"
        or provisioned.applied_revision != provisioned.desired_revision
        or provisioned.source_generation != source.generation):
        raise HTTPException(status_code=409, detail="Enable this source from connector settings before collecting")
    revision = provisioned.desired_revision
    if not await provisioning.require_collection_fence(session, source, source.generation, revision, lock=True, multi_workspace_enabled=multi_workspace_enabled, scope=scope):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    if provisioned.execution_backend == "native":
        return await _queue_native_collection(
            session, request, source_id, source, revision, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    # MCP has no URL to validate: its saved calls are checked by mcp_collection.validate at run time.
    if not is_native_provider(source.provider) and source.type != mcp_collection.PROVIDER_ID:
        try:
            data = registry.validate(source)
            await session.rollback()
            await validate_public_url(data["url"])
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="Configure and validate the connector before syncing") from exc
    await session.rollback()
    await _owner_access(session, request, scope, expected=access_fence)
    result = await wake_packaged_collection(
        session, source_id=source_id, source_generation=source.generation,
        connector_revision=revision, settings=settings, timeout_seconds=75,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if result.outcome == "acknowledged":
        return ManualSyncResult(run_id=result.run_id, batch_id=result.batch_id, status=result.status or "queued")
    if result.outcome == "deferred":
        raise HTTPException(status_code=409, detail="A collection wake is already pending")
    if not (await resolve_n8n(session, settings, workspace_id=scope.workspace_id)).webhook_token:
        raise HTTPException(status_code=503, detail="Manual n8n trigger authentication is not configured")
    if await sources.record_collection_result(
        session, source_id, source.generation, datetime.now(UTC), "n8n_unavailable",
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        current = await sources.get_source_fence(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        drafts = [make_source_change(current.id, current.generation, current.status, scope=scope)] if current is not None else []
        await commit_with_replay(session, drafts, access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if result.outcome == "ambiguous":
        raise HTTPException(status_code=503, detail="n8n collection wake outcome is unknown")
    raise HTTPException(status_code=503, detail="n8n collection workflow is unavailable or failed")


@router.get("/{source_id}/collection-requests/{request_id}", response_model=CollectionRequestRead,
            dependencies=[Depends(module_dependency("connectors"))])
async def read_collection_request(
    source_id: UUID, request_id: UUID, session: Session, request: Request,
    _owner: Annotated[WorkspaceContext, Depends(require_workspace_read)],
) -> CollectionRequestRead:
    """Owner poll of one durable collection request; foreign or absent ids share one 404."""
    if _owner.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return await connectors_public.get_collection_request(
        session, _owner, source_id, request_id,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)


@router.post("/{source_id}/validate", response_model=ConnectorState)
async def validate_source(
    source_id: UUID,
    request: Request, session: Session,
    payload: CollectionFence,
    authorization: Annotated[str | None, Header()] = None,
) -> ConnectorState:
    """Validate only generic collector configuration; native providers use owner draft validation.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="native_collection_required")
    if not await provisioning.require_validation_fence(
        session, source, payload.source_generation, payload.connector_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Connector validation fence is stale")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.rollback()
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    try:
        data = registry.validate(source)
        await session.rollback()
        await validate_public_url(data["url"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    cursor = await ingestion.get_source_cursor(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    current = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if not await provisioning.require_validation_fence(
        session, current, payload.source_generation, payload.connector_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Connector validation fence changed during validation")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    result = registry.sync(source, cursor)
    result["workspace_id"] = source.workspace_id
    result["connector_revision"] = payload.connector_revision
    return ConnectorState(**result)


@router.get("/{source_id}/rss", response_model=ConnectorPreview)
async def preview_rss(
    source_id: UUID,
    session: Session,
    request: Request,
    source_generation: int,
    connector_revision: int,
    admission_request_id: UUID,
    admission_token: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> ConnectorPreview:
    """Fetch a bounded generic RSS preview, excluding feeds owned by named native adapters.

    Fetch-only: the admission token authorizes the fetch and is re-proved under lock before and
    after the browser call (409 when stale or replayed). n8n then submits the records through
    /sync or /no-changes, which settle the request in the same commit as the batch.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="native_collection_required")
    if not await provisioning.require_collection_fence(
        session, source, source_generation, connector_revision, lock=True,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.rollback()
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    try:
        data = registry.validate(source)
        if source.type != "rss":
            raise ValueError("RSS/Atom source required")
        await session.rollback()
        await validate_public_url(data["url"])
        cursor = await ingestion.get_source_cursor(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        settings = request.app.state.settings
        token = settings.browser_shared_token.get_secret_value()
        if not token:
            raise HTTPException(status_code=503, detail="Browser collector is not configured")
        await session.rollback()
        await lock_access_fence(session, scope=scope, expected=access_fence,
                                multi_workspace_enabled=multi_workspace_enabled)
        current_fence, _row, _slots = await provisioning.lock_connector(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            expected_access_fence=access_fence,
        )
        if (current_fence is None or current_fence.generation != source_generation
                or not await provisioning.require_collection_fence(
                    session, source, source_generation, connector_revision,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)):
            raise HTTPException(status_code=409, detail="Original RSS configuration changed")
        ref = CollectionRequestRef(request_id=admission_request_id, admission_token=admission_token)
        await _prove_admission(session, ref, source_id=source_id, source_generation=source_generation,
                               connector_revision=connector_revision, scope=scope)
        await _collector_current(session, source_id, collector_token, scope=scope,
                                 multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        await session.rollback()
        async with httpx.AsyncClient(timeout=65) as client:
            response = await client.post(
                f"{str(settings.browser_service_url).rstrip('/')}/rss",
                json=RSSRequest(url=data["url"], cursor=cursor).model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
        result = response.json()
        result["source_generation"] = source_generation
        result["connector_revision"] = connector_revision
        await lock_access_fence(session, scope=scope, expected=access_fence,
                                multi_workspace_enabled=multi_workspace_enabled)
        await provisioning.lock_connector(session, source_id, scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled,
                                          expected_access_fence=access_fence)
        current = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        if not await provisioning.require_collection_fence(
            session, current, source_generation, connector_revision, lock=True,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        ):
            raise HTTPException(status_code=409, detail="Connector collection fence changed during collection")
        await _prove_admission(session, ref, source_id=source_id, source_generation=source_generation,
                               connector_revision=connector_revision, scope=scope)
        await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
        await session.rollback()
        return ConnectorPreview.model_validate(result)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=422, detail="RSS source could not be collected") from exc


@router.post("/{source_id}/collection-admission", response_model=CollectionAdmissionRead)
async def admit_collection(
    source_id: UUID,
    payload: CollectionAdmissionRequest,
    request: Request, session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> CollectionAdmissionRead:
    """Admit one managed-n8n run before any provider I/O; 409 when stale or busy.

    Same collector bearer as /sync. The returned token is fenced to this request and slot; /sync
    and /no-changes must carry it, and a stale or replayed token is refused there.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    try:
        _, scope, _ = await _collector(session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        await session.rollback()  # the MCP workflow holds only the distinct mcp:collect bearer
        _, scope, _ = await _mcp_collector(session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled)
        # The mcp:collect bearer is valid only for an MCP source; every other type needs the collector bearer.
        source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
        if source.type != mcp_collection.PROVIDER_ID:
            raise HTTPException(status_code=403, detail="MCP collector bearer is valid only for MCP sources")
    await session.rollback()  # admission takes its own fence -> Source -> request locks
    return await connectors_public.admit_managed_collection(
        session, scope, source_id, payload, trigger=payload.trigger, multi_workspace_enabled=multi_workspace_enabled)


@router.post("/{source_id}/sync", response_model=Receipt, status_code=202)
async def receive_connector_batch(
    source_id: UUID,
    payload: ManagedConnectorReceipt,
    request: Request, session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> Receipt:
    """Accept only generic collector batches after bearer and revision checks; native writes use owner receipt APIs.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="native_collection_required")
    if not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True, backend_revision=payload.backend_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    try:
        registry.validate(source)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    batch = ReceiveBatch(
        source_id=source_id,
        source_generation=payload.source_generation,
        connector_revision=payload.connector_revision,
        batch_key=f"connector-request:{payload.admission_request_id}",
        cursor_before=payload.cursor_before,
        cursor_after=payload.cursor_after,
        records=[record.model_dump() for record in payload.records],
    )
    await session.rollback()
    await lock_access_fence(session, scope=scope, expected=access_fence,
                            multi_workspace_enabled=multi_workspace_enabled)
    # Ingestion re-proves the running request, unexpired slot and token under lock (409 when stale)
    # and settles the request in the same commit as the batch.
    return await ingestion.receive_connector_batch(
        session, batch, collector_token, multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        request_ref=CollectionRequestRef(
            request_id=payload.admission_request_id, admission_token=payload.admission_token))


@router.post("/{source_id}/no-changes", response_model=ManualSyncResult)
async def acknowledge_no_changes(
    source_id: UUID,
    payload: ManagedNoChanges,
    request: Request, session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> ManualSyncResult:
    """Record only generic collector no-change status under a verified admission; native providers persist a native receipt and cursor atomically.

    Ingestion proves the admission token, settles the request and frees the slot in one commit; a
    stale or replayed token is a 409. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="native_collection_required")
    if not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True, backend_revision=payload.backend_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    await session.rollback()
    await lock_access_fence(session, scope=scope, expected=access_fence, multi_workspace_enabled=multi_workspace_enabled)
    await ingestion.accept_collection_no_changes(
        session, source_id=source_id, source_generation=payload.source_generation,
        connector_revision=payload.connector_revision,
        request_ref=CollectionRequestRef(
            request_id=payload.admission_request_id, admission_token=payload.admission_token),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return ManualSyncResult(status="no_changes")


@router.post("/{source_id}/crawl", response_model=CrawlResult, status_code=202,
             dependencies=[Depends(require_budget("crawl", 10, 60))])
async def submit_crawl(
    source_id: UUID,
    payload: ManagedCrawlRequest,
    session: Session,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CrawlResult:
    """Queue a source-scoped crawl request after validating its collection fence.

    The queued crawl run is the request's outcome: ingestion re-proves the admission under lock
    (409 when stale or replayed) and settles it against that run in the queueing commit.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    settings = request.app.state.settings
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if source.type != "web" or is_native_provider(source.provider) or not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True, backend_revision=payload.backend_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    ):
        raise HTTPException(status_code=409, detail="Active web source required")
    await _collector_current(session, source_id, collector_token, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    try:
        config = registry.configuration(source)
        if config.url is None:
            raise ValueError("Web connector URL is required")
        await session.rollback()
        url = await validate_public_url(str(config.url))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid or unsafe source URL") from exc
    if payload.source_id != source_id or str(payload.url) != url:
        raise HTTPException(status_code=422, detail="Crawl target must match the configured source URL")
    if payload.mode != ("playwright" if config.js_render else "http"):
        raise HTTPException(status_code=422, detail="Crawl mode must match the configured source")
    if (
        payload.max_pages > config.max_pages
        or payload.max_depth > config.max_depth
        or payload.timeout_seconds > config.timeout_seconds
    ):
        raise HTTPException(status_code=422, detail="Crawl request exceeds the configured source budget")
    if not settings.browser_shared_token.get_secret_value():
        raise HTTPException(status_code=503, detail="Browser collector is not configured")
    cursor = await ingestion.get_source_cursor(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    await session.rollback()
    await lock_access_fence(session, scope=scope, expected=access_fence,
                            multi_workspace_enabled=multi_workspace_enabled)
    receipt = await ingestion.queue_connector_crawl(
        session,
        source_id,
        payload.source_generation,
        payload.connector_revision,
        cursor,
        {
            "url": url,
            "mode": payload.mode,
            "max_pages": payload.max_pages,
            "max_depth": payload.max_depth,
            "timeout_seconds": payload.timeout_seconds,
        },
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        request_ref=CollectionRequestRef(
            request_id=payload.admission_request_id, admission_token=payload.admission_token),
    )
    return CrawlResult(run_id=receipt.run_id)
