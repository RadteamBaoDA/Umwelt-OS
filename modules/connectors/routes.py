import asyncio
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar, cast
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.realtime import commit_with_replay, make_source_change
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.connectors import mcp as mcp_collection
from modules.connectors import provider_terms, provisioning, registry, scheduler
from modules.connectors import public as connectors_public
from modules.connectors.backends import FIXED_SCOPE_PROVIDERS
from modules.connectors.collection_schemas import (
    CollectionAdmissionRead,
    CollectionAdmissionRequest,
    CollectionRequestRead,
    CollectionRequestRef,
    ManagedConnectorReceipt,
    ManagedNoChanges,
)
from modules.connectors.github import oauth as github_oauth
from modules.connectors.github.adapter import collect_github_segment
from modules.connectors.github.schemas import GitHubHintClaimProof, project_github_source_config
from modules.connectors.github.sync import validate_github_segment
from modules.connectors.models import (
    ConnectorProvisioning,
    ConnectorWorldCredential,
    GithubOAuthGrant,
)
from modules.connectors.provider_specs import TERMS_INELIGIBLE
from modules.connectors.public import (
    AgentBrowserGrantPatch,
    CollectionFence,
    ConnectorConfigurationRequest,
    ConnectorPreview,
    CrawlRequest,
    CrawlResult,
    NativeCredentialSnapshot,
    ProviderRateLimited,
    RSSRequest,
    get_native_credential_snapshot,
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
    ConnectorCollectionLease,
    NativeCollectionBatch,
    NativeCollectionReceipt,
    Receipt,
    ReceiveBatch,
    classify_telegram_probe,
)
from modules.settings.public import module_dependency, module_is_enabled
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource, SourceFence

router = APIRouter(prefix="/api/v1/connectors/sources", tags=["connectors"])
Session = Annotated[AsyncSession, Depends(get_session)]
operator_router = APIRouter(prefix="/api/v1/operator/connectors", tags=["connectors"])
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
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
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

    operation_id = uuid4()
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
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
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

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    token, scope, access_fence = await _mcp_collector(
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
    runtime = getattr(request.app.state, "mcp_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="MCP runtime is unavailable")
    await session.rollback()
    raise HTTPException(status_code=503, detail="Scoped MCP collector contract is pending")


@router.get("/{source_id}/agent-browser-grant", dependencies=[Depends(module_dependency("connectors"))])
async def read_agent_browser_grant(
    source_id: UUID, session: Session, request: Request, _owner: OwnerWrite
) -> dict[str, object]:
    """Return the current source-bound browser opt-in, without exposing connector credentials.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    scope = await resolve_agent_browser_scope(session, _owner.user_id, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if scope is None:
        return {"available": False, "enabled": False}
    return {"available": True, **scope.__dict__}


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
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    try:
        scope = await update_agent_browser_grant_in_uow(
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
    return {"available": True, **scope.__dict__}


class ProviderFetchRequest(BaseModel):
    """Accept only source-generation and connector-revision fences from n8n."""
    model_config = ConfigDict(extra="forbid")

    source_generation: int = Field(ge=1)
    connector_revision: int = Field(ge=1)
    backend_revision: int | None = Field(default=None, ge=1)


class ProviderFetchRead(BaseModel):
    """Expose verified receipt workspace/status without content, cursor or credential material."""
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    status: Literal["succeeded", "queued", "no_changes", "rate_limited"]
    batch_id: UUID | None = None
    run_id: UUID | None = None
    received_update_count: int = Field(ge=0, le=500)
    record_count: int = Field(ge=0, le=500)
    coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
    next_eligible_at: datetime | None = None

    @field_validator("next_eligible_at")
    @classmethod
    def aware_deadline(cls, value: datetime | None) -> datetime | None:
        """Reject naive retry times so clients do not misread provider deadlines."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_eligible_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


class ProviderAdmissionBusy(RuntimeError):
    """Signal that the shared single-provider network permit is held by another collector."""


_T = TypeVar("_T")


async def _await_with_github_segment_deadline(  # noqa: UP047  # keep TypeVar/TypeAlias spelling; PEP 695 rewrite is style-only
    operation: Awaitable[_T], *, deadline: float | None
) -> _T:
    """Bound one pre-lease await by the shared GitHub segment deadline.

    A missing deadline leaves other providers' admission behavior unchanged. When the
    deadline expires, the awaited operation is cancelled and its caller rolls back;
    after lease acquisition, the route releases only the exact active lease token.
    """
    if deadline is None:
        return await operation
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(operation, timeout=remaining)


GITHUB_RECONNECT_DETAIL = "GitHub grant is expired or requires reconnection"


def _collection_error_code(exc: BaseException) -> str:
    """Map auth-type failures (HTTP 401/403, Telegram credentials rejected, GitHub reconnect) to a reconnect-needed code."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return "provider_unauthorized"
    if getattr(exc, "code", None) == "telegram_credentials_rejected":
        return "provider_unauthorized"
    if isinstance(exc, HTTPException) and exc.detail == GITHUB_RECONNECT_DETAIL:
        return "provider_unauthorized"
    return "provider_collection_failed"


async def _release_failed_collection(
    session: AsyncSession,
    lease: ConnectorCollectionLease,
    *,
    error_code: str, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Best-effort release after failure without masking the original outcome.

    Rollback clears failed SQLAlchemy transaction state before the ingestion
    owner checks the lease token. Cleanup is bounded; an unreleased reservation
    still expires under its existing lease policy.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    try:
        async with asyncio.timeout(3):
            await session.rollback()
            await ingestion.release_connector_collection(session, lease, error_code=error_code, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    except BaseException:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        return


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
    The actual rollout flag and scoped module gates remain required.
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
    await provisioning.require_n8n_backend(session, source_id)
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


async def _native_send_fence(
    session: AsyncSession, source: ConnectorSource, lease: ConnectorCollectionLease,
    collector_token: str, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
    native_snapshot: NativeCredentialSnapshot | None = None,
    world_operation_id: UUID | None = None, github_fence: object = None,
    github_binding: tuple[UUID, int] | None = None,
) -> None:
    """Revalidate original native authority in parent order and release SQL before every send.

    The actual pre-I/O AccessFence/SourceFence and committed lease remain immutable.
    Lock admission/Source/provisioning/sorted slots, then applicable provider credential,
    then Ingestion's exact bearer/state reservation through its held-parent public seam.
    No current scope reconstruction, state ORM import, health/cursor mutation or I/O occurs.
    False/missing/changed proof raises 409; authorization/storage failures propagate.
    """
    try:
        current_source, row, _slots = await provisioning.lock_connector(
            session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
        )
        if (current_source != source_fence or row is None
                or not await provisioning.require_collection_fence(
                    session, source, lease.source_generation, lease.connector_revision,
                    scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                )):
            raise HTTPException(status_code=409, detail="Original provider Source/configuration changed")
        if native_snapshot is not None:
            current_native = await get_native_credential_snapshot(
                session, source.id, source_generation=lease.source_generation,
                connector_revision=lease.connector_revision, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if current_native != native_snapshot or native_snapshot.access_fence != access_fence:
                raise HTTPException(status_code=409, detail="Original native credential changed")
        if source.provider == "alpha_vantage":
            from modules.connectors import public as connectors

            if (not isinstance(world_operation_id, UUID)
                    or not await connectors.validate_world_credential_operation_in_uow(
                        session, source.id, source_generation=lease.source_generation,
                        connector_revision=lease.connector_revision,
                        expected_operation_id=world_operation_id,
                        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                        access_fence=access_fence, source_fence=source_fence,
                    )):
                raise HTTPException(status_code=409, detail="Original world credential changed")
        # Collector credentials are a later owner lock than provider credentials and
        # must precede the GitHub grant. The held validator below then acquires only
        # SourceIngestionState, preserving the accepted token -> grant -> state order.
        await ingestion.lock_source_credentials_in_uow(
            session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_fence,
        )
        if github_fence is not None:
            from modules.connectors import public as connectors

            current_binding = await connectors.lock_github_binding_fence_in_uow(
                session, source.id, source_generation=lease.source_generation,
                connector_revision=lease.connector_revision, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                access_fence=access_fence, source_fence=source_fence,
            )
            grant = await session.get(GithubOAuthGrant, source.id, populate_existing=True)
            if (current_binding != github_fence or grant is None
                    or github_binding != (grant.operation_id, grant.token_revision)):
                raise HTTPException(status_code=409, detail="Original GitHub grant changed")
        if not await ingestion.validate_connector_collection_in_uow(
            session, lease, collector_token=collector_token, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_fence,
        ):
            raise HTTPException(status_code=409, detail="Original collection reservation changed")
    finally:
        await session.rollback()


async def _provider_cooldown(request: Request, provider: str) -> datetime | None:
    """Read the fixed-provider shared retry deadline; Redis failure closes collection."""
    try:
        raw = await request.app.state.redis.get(f"connectors:provider:cooldown:{provider}")
        if raw is None:
            return None
        milliseconds = int(raw)
        return datetime.fromtimestamp(milliseconds / 1000, UTC) if milliseconds > 0 else None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="provider_rate_state_unavailable") from exc


async def _extend_provider_cooldown(request: Request, provider: str, deadline: datetime) -> datetime:
    """Atomically retain the later UTC deadline across all sources using one fixed provider."""
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("Provider retry deadline must be timezone-aware")
    deadline_ms = int(deadline.astimezone(UTC).timestamp() * 1000)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    script = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local incoming = tonumber(ARGV[1])
if incoming > current then
  local ttl = incoming - tonumber(ARGV[2])
  if ttl < 1 then ttl = 1 end
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ttl)
  current = incoming
end
return current
"""
    try:
        stored = int(await request.app.state.redis.eval(
            script, 1, f"connectors:provider:cooldown:{provider}", deadline_ms, now_ms
        ))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="provider_rate_state_unavailable") from exc
    return datetime.fromtimestamp(stored / 1000, UTC)


async def _acquire_github_network_permit(request: Request) -> str | None:
    """Acquire one cluster-shared GitHub network slot with an expiry beyond the segment deadline."""
    token = secrets.token_urlsafe(24)
    try:
        acquired = await request.app.state.redis.set("connectors:provider:active:github", token, nx=True, ex=75)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="provider_admission_unavailable") from exc
    return token if acquired else None


async def _release_github_network_permit(request: Request, token: str) -> None:
    """Release only the GitHub network slot still owned by this collector token."""
    script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
    try:
        await request.app.state.redis.eval(script, 1, "connectors:provider:active:github", token)
    except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
        # The bounded permit expires automatically; never release another collector's token.
        return


def _provider_fetch_read(receipt: NativeCollectionReceipt, next_eligible_at: datetime | None = None) -> ProviderFetchRead:
    """Expose actual original receipt workspace/status without private cursor/content."""
    return ProviderFetchRead(
        workspace_id=receipt.workspace_id,
        status=receipt.status,
        batch_id=receipt.batch_id,
        run_id=receipt.run_id,
        received_update_count=receipt.received_update_count,
        record_count=receipt.record_count,
        coverage=receipt.coverage,
        next_eligible_at=next_eligible_at or receipt.next_eligible_at,
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

    Shared provider cooldowns return before reserving a lease. A provider retry
    deadline releases the active reservation and returns rate_limited without
    publishing ingestion receipt, cursor, or success health. Telegram pagination
    updates active_lease after each accepted page so failures and cancellation can
    release the current reservation rather than an already-consumed token. GitHub's
    single 60-second segment deadline starts before connector fencing and covers
    cooldown, admission, reservation, one selected GET, and receipt acceptance. A due durable
    hint may select one current-object GET; only its proof and exact claim can acknowledge it.
    GitHub requests decrypt only a source/revision-bound grant, hold one cluster-shared
    network permit, and revalidate raw proof in Ingestion before durable acceptance.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    collector_token, scope, access_fence = await _collector(
        session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    if not await module_is_enabled(session, "ingestion", multi_workspace_enabled=multi_workspace_enabled, scope=scope):
        raise HTTPException(status_code=404, detail="Provider collection is unavailable")
    segment_deadline = (
        asyncio.get_running_loop().time() + 60 if source.provider == "github" else None
    )
    if not is_native_provider(source.provider):
        raise HTTPException(status_code=409, detail="Native provider is not configured")
    if source.provider in FIXED_SCOPE_PROVIDERS:  # free providers run only through the gated shared executor
        raise HTTPException(status_code=409, detail="Provider is collected by the scheduler only")
    try:
        registry.validate(source)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Provider configuration is invalid") from exc
    try:
        if not await _await_with_github_segment_deadline(
            provisioning.require_collection_fence(
                session, source, payload.source_generation,
                payload.connector_revision, lock=True, backend_revision=payload.backend_revision,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            ),
            deadline=segment_deadline,
        ):
            raise HTTPException(status_code=409, detail="Connector collection fence is stale")
        await _await_with_github_segment_deadline(
            session.rollback(), deadline=segment_deadline
        )
        now = datetime.now(UTC)
        cooldown = await _await_with_github_segment_deadline(
            _provider_cooldown(request, source.provider), deadline=segment_deadline
        )
        if cooldown is not None and cooldown > now:
            return ProviderFetchRead(workspace_id=scope.workspace_id, 
                status="rate_limited", batch_id=None, run_id=None,
                received_update_count=0, record_count=0,
                coverage="pending_updates_only" if source.provider == "telegram" else "returned_snapshot",
                next_eligible_at=cooldown,
            )
        lease = await _await_with_github_segment_deadline(
            ingestion.acquire_connector_collection(
                session,
                source_id=source_id,
                source_generation=payload.source_generation,
                connector_revision=payload.connector_revision,
                collector_token=collector_token,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            ),
            deadline=segment_deadline,
        )
    except TimeoutError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Provider collection admission exceeded its deadline") from exc
    source_fence = await sources.get_source_fence(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source_fence is None or source_fence.generation != lease.source_generation
            or source_fence.workspace_id != lease.workspace_id
            or access_fence.configuration_revision != lease.configuration_revision):
        await session.rollback()
        raise HTTPException(status_code=409, detail="Original collection reservation changed")
    await session.rollback()
    active_lease = [lease]
    github_binding: tuple[UUID, int] | None = None
    github_permit_token: str | None = None
    github_hint_claim = None
    if source.provider == "github":
        from modules.connectors import public as connectors

        try:
            github_hint_claim = await _await_with_github_segment_deadline(
                connectors.claim_github_hint(
                    session, source_id=source.id, source_generation=payload.source_generation,
                    connector_revision=payload.connector_revision,
                    multi_workspace_enabled=multi_workspace_enabled, scope=scope,
                ),
                deadline=segment_deadline,
            )
        except BaseException:
            await _release_failed_collection(
                session, active_lease[0], error_code="provider_collection_failed",
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
            raise
    try:
        collection_timeout = (
            asyncio.timeout_at(segment_deadline)
            if segment_deadline is not None else asyncio.timeout(60)
        )
        async with collection_timeout:
            if source.provider == "telegram":
                receipt, eligible = await _collect_telegram_page(
                    session, request, source, lease, collector_token, active_lease, source_fence=source_fence,
                    access_fence=access_fence, multi_workspace_enabled=multi_workspace_enabled, scope=scope,
                )
                if isinstance(receipt, ProviderFetchRead):
                    return receipt
            else:
                collected_at = datetime.now(UTC)
                github_proof = None
                github_validated = None
                if source.provider in {"youtube", "arxiv"}:
                    from modules.connectors.providers.feed_catalog import collect_provider_feed

                    page = await collect_provider_feed(
                        source, collected_at=collected_at,
                        session_factory=request.app.state.session_factory,
                        before_request=lambda: _native_send_fence(
                            session, source, lease, collector_token, scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled,
                            access_fence=access_fence, source_fence=source_fence,
                        ),
                    )
                elif source.provider == "huggingface":
                    from modules.connectors.providers.research import collect_huggingface_models

                    page = await collect_huggingface_models(
                        source, collected_at=collected_at,
                        before_request=lambda: _native_send_fence(
                            session, source, lease, collector_token, scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled,
                            access_fence=access_fence, source_fence=source_fence,
                        ),
                    )
                elif source.provider == "github":
                    from modules.connectors import public as connectors

                    await provisioning.lock_connector(
                        session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                        expected_access_fence=access_fence,
                    )
                    fence = await connectors.lock_github_binding_fence_in_uow(
                        session, source.id, source_generation=payload.source_generation,
                        connector_revision=payload.connector_revision,
                        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                        access_fence=access_fence, source_fence=source_fence,
                    )
                    if fence is None:
                        raise HTTPException(status_code=409, detail=GITHUB_RECONNECT_DETAIL)
                    grant = await session.scalar(
                        select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source.id).with_for_update()
                    )
                    if (
                        grant is None or grant.state != "ready" or grant.encrypted_tokens is None
                        or grant.source_generation != source.generation
                        or grant.configuration_revision != payload.connector_revision
                        or grant.expires_at is None or grant.expires_at <= datetime.now(UTC)
                    ):
                        raise HTTPException(status_code=409, detail=GITHUB_RECONNECT_DETAIL)
                    token_revision = grant.token_revision
                    grant_operation = grant.operation_id
                    key = request.app.state.settings.connector_credential_encryption_key.get_secret_value()
                    token_pair = github_oauth._open_token_cipher(
                        key, grant.encrypted_tokens, source.id, grant.operation_id,
                        grant.source_generation, grant.configuration_revision,
                    )
                    access_token = token_pair.get("access_token")
                    if not isinstance(access_token, str) or not access_token:
                        raise ValueError("github_grant_unavailable")
                    github_binding = (grant_operation, token_revision)
                    github_config = project_github_source_config(source.configuration)
                    await session.rollback()
                    async def before_github_request() -> None:
                        """Check original lease and exact grant proof, release SQL, then reserve network capacity."""
                        cooldown_at = await _provider_cooldown(request, "github")
                        if cooldown_at is not None and cooldown_at > datetime.now(UTC):
                            raise ProviderRateLimited(cooldown_at)
                        await _native_send_fence(
                            session, source, lease, collector_token, scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled,
                            access_fence=access_fence, source_fence=source_fence,
                            github_fence=fence, github_binding=github_binding,
                        )
                        nonlocal github_permit_token
                        github_permit_token = await _acquire_github_network_permit(request)
                        if github_permit_token is None:
                            raise ProviderAdmissionBusy("GitHub provider slot is busy")

                    github_proof = await collect_github_segment(
                        github_config, access_token, fence=fence,
                        cursor_before=lease.cursor_before, collected_at=collected_at,
                        before_request=before_github_request,
                        hint_claim=(
                            GitHubHintClaimProof.model_validate(github_hint_claim.model_dump(mode="python"))
                            if github_hint_claim is not None else None
                        ),
                    )
                    github_validated = validate_github_segment(
                        fence, github_config, lease.cursor_before, github_proof,
                        collected_at=collected_at,
                    )
                    page = None
                else:
                    if source.provider in {"alpha_vantage", "open_meteo"}:
                        from modules.connectors.providers.world_data import collect_world_data
                        original_world_operation_id: UUID | None = None
                        world_capture_seen = False

                        async def before_world_request(credential_operation_id: UUID | None) -> None:
                            """Capture the pre-I/O Alpha operation once and compare it on every GET."""
                            nonlocal original_world_operation_id, world_capture_seen
                            if source.provider == "alpha_vantage":
                                if not isinstance(credential_operation_id, UUID):
                                    raise HTTPException(status_code=409, detail="Original Alpha credential capture is missing")
                                if world_capture_seen and credential_operation_id != original_world_operation_id:
                                    raise HTTPException(status_code=409, detail="Original Alpha credential operation changed")
                                original_world_operation_id = credential_operation_id
                                world_capture_seen = True
                            elif credential_operation_id is not None:
                                raise HTTPException(status_code=409, detail="Unexpected world credential capture")
                            await _native_send_fence(
                                session, source, lease, collector_token, scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled,
                                access_fence=access_fence, source_fence=source_fence,
                                world_operation_id=original_world_operation_id,
                            )

                        page = await collect_world_data(
                            source, collected_at=collected_at,
                            settings=request.app.state.settings,
                            session=session,
                            redis=request.app.state.redis,
                            scope=scope, multi_workspace_enabled=multi_workspace_enabled,
                            access_fence=access_fence, source_fence=source_fence,
                            before_request=before_world_request,
                        )
                    else:
                        from modules.connectors.providers.social import collect_github_releases

                        page = await collect_github_releases(
                            source, collected_at=collected_at,
                            before_request=lambda: _native_send_fence(
                                session, source, lease, collector_token, scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled,
                                access_fence=access_fence, source_fence=source_fence,
                            ),
                        )
                if source.provider == "alpha_vantage" and page is not None:
                    if (not world_capture_seen or original_world_operation_id is None
                            or page.credential_operation_id != original_world_operation_id):
                        raise HTTPException(status_code=409, detail="Alpha credential capture changed during collection")
                    await _native_send_fence(
                        session, source, lease, collector_token, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                        access_fence=access_fence, source_fence=source_fence,
                        world_operation_id=original_world_operation_id,
                    )
                eligible = None
                if page is not None and page.next_eligible_at is not None:
                    eligible = await _extend_provider_cooldown(request, source.provider, page.next_eligible_at)
                    # Rate limited pages never publish an ingestion receipt, cursor, or success health.
                    await ingestion.release_connector_collection(
                        session, lease, error_code="provider_rate_limited",
                        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
                    )
                    return ProviderFetchRead(workspace_id=scope.workspace_id, 
                        status="rate_limited", batch_id=None, run_id=None,
                        received_update_count=0, record_count=0,
                        coverage=page.coverage, next_eligible_at=eligible,
                    )
                if github_binding is not None:
                    await _native_send_fence(
                        session, source, lease, collector_token, scope=scope,
                        multi_workspace_enabled=multi_workspace_enabled,
                        access_fence=access_fence, source_fence=source_fence,
                        github_fence=fence, github_binding=github_binding,
                    )
                batch_coverage: Literal["returned_snapshot", "pending_updates_only", "truncated"]
                if github_validated is not None:
                    batch_records, batch_coverage = github_validated.records, github_validated.coverage
                else:
                    assert page is not None
                    batch_records, batch_coverage = page.records, page.coverage
                native_batch = NativeCollectionBatch(
                    source_id=source.id,
                    source_generation=source.generation,
                    connector_revision=payload.connector_revision,
                    lease_token=lease.token,
                    cursor_before=lease.cursor_before,
                    cursor_after=github_validated.cursor_after if github_validated is not None else lease.cursor_before,
                    records=list(batch_records), telegram_deliveries=(),
                    telegram_raw_deliveries=(), coverage=batch_coverage,
                    github_segment=github_proof,
                    collected_at=collected_at,
                )
                receipt = await ingestion.accept_native_collection(
                    session, native_batch, collector_token=collector_token, lease=lease,
                    multi_workspace_enabled=multi_workspace_enabled, scope=scope,
                    expected_native_operation_id=None,
                    expected_world_credential_operation_id=original_world_operation_id if source.provider == "alpha_vantage" else None,
                )
                return _provider_fetch_read(receipt, eligible)
        return _provider_fetch_read(receipt, eligible)
    except ProviderAdmissionBusy:
        eligible = datetime.now(UTC) + timedelta(seconds=1)
        await _release_failed_collection(
            session, active_lease[0], error_code="provider_admission_busy",
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        return ProviderFetchRead(workspace_id=scope.workspace_id, 
            status="rate_limited", batch_id=None, run_id=None,
            received_update_count=0, record_count=0,
            coverage="returned_snapshot", next_eligible_at=eligible,
        )
    except ProviderRateLimited as exc:
        deadline = await _extend_provider_cooldown(request, source.provider, exc.next_eligible_at)
        await _release_failed_collection(
            session, active_lease[0], error_code="provider_rate_limited",
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        return ProviderFetchRead(workspace_id=scope.workspace_id, 
            status="rate_limited", batch_id=None, run_id=None,
            received_update_count=0, record_count=0,
            coverage="pending_updates_only" if source.provider == "telegram" else "returned_snapshot",
            next_eligible_at=deadline,
        )
    except HTTPException as exc:
        await _release_failed_collection(
            session, active_lease[0], error_code=_collection_error_code(exc),
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        raise
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        await _release_failed_collection(
            session, active_lease[0], error_code=_collection_error_code(exc),
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        raise HTTPException(status_code=503, detail="Provider collection failed") from exc
    except BaseException:
        await _release_failed_collection(
            session, active_lease[0], error_code="provider_collection_failed",
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        raise
    finally:
        if github_permit_token is not None:
            await _release_github_network_permit(request, github_permit_token)


async def _collect_telegram_page(
    session: AsyncSession,
    request: Request,
    source: ConnectorSource,
    lease: ConnectorCollectionLease,
    collector_token: str,
    active_lease: list[ConnectorCollectionLease],
    *, scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
    source_fence: SourceFence,
) -> tuple[NativeCollectionReceipt | ProviderFetchRead, datetime | None]:
    """Probe Telegram without an offset and continue only after durable page receipts.

    Update active_lease whenever continuation reserves a new page; this lets the
    route release the matching reservation on provider errors or cancellation.
    A provider retry deadline is shared across Telegram sources and ends the run
    without accepting a page or advancing its cursor.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Service bearer capability remains distinct from principal scope.
    """
    from modules.connectors.credentials import decrypt_native_token
    from modules.connectors.providers.telegram import fetch_telegram_updates, map_telegram_update

    await provisioning.lock_connector(
        session, source.id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
    )
    snapshot = await get_native_credential_snapshot(
        session, source.id, source_generation=lease.source_generation,
        connector_revision=lease.connector_revision,
        multi_workspace_enabled=multi_workspace_enabled, scope=scope,
    )
    if snapshot is None or snapshot.state != "ready" or not snapshot.encrypted_token or not snapshot.verified_bot_id:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Native Telegram credential is not ready")
    try:
        token = decrypt_native_token(
            request.app.state.settings.connector_credential_encryption_key.get_secret_value(), snapshot
        )
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Native Telegram credential is unavailable") from exc
    await session.rollback()
    cursor = await ingestion.read_telegram_collection_state(session, lease, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    offset: int | None = None
    total_updates = 0
    total_transport_bytes = 0
    last_receipt: NativeCollectionReceipt | None = None
    for page_number in range(5):
        remaining_bytes = 25 * 1024 * 1024 - total_transport_bytes
        if remaining_bytes <= 0:
            raise HTTPException(status_code=422, detail="Telegram trigger byte limit exceeded")
        try:
            await _native_send_fence(
                session, source, lease, collector_token, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                source_fence=source_fence, native_snapshot=snapshot,
            )
            page = await fetch_telegram_updates(
                token, offset=offset,
                remaining_bytes=min(10 * 1024 * 1024, remaining_bytes),
                before_request=lambda: _native_send_fence(
                    session, source, lease, collector_token, scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
                    source_fence=source_fence, native_snapshot=snapshot,
                ),
            )
        except ProviderRateLimited as exc:
            deadline = await _extend_provider_cooldown(request, source.provider or "telegram", exc.next_eligible_at)
            await ingestion.release_connector_collection(
                session, lease, error_code="provider_rate_limited",
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
            return ProviderFetchRead(workspace_id=scope.workspace_id, 
                status="rate_limited", batch_id=None, run_id=None,
                received_update_count=0, record_count=0,
                coverage="pending_updates_only", next_eligible_at=deadline,
            ), deadline
        if page.transport_bytes > remaining_bytes:
            raise HTTPException(status_code=422, detail="Telegram trigger byte limit exceeded")
        total_transport_bytes += page.transport_bytes
        if total_updates + len(page.deliveries) > 500:
            raise HTTPException(status_code=422, detail="Telegram trigger update limit exceeded")
        total_updates += len(page.deliveries)
        classification = classify_telegram_probe(
            cursor, page.deliveries, received_at=page.collected_at,
            verified_bot_id=snapshot.verified_bot_id,
        )
        if classification.conflict_code is not None:
            raise HTTPException(status_code=409, detail="telegram_stream_conflict")
        replay_ids = set(classification.replay_update_ids)
        if page.deliveries and len(replay_ids) == len(page.deliveries):
            await ingestion.release_connector_collection(session, lease, error_code=None, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
            if page_number == 4 or cursor is None or cursor.last_update_id >= 2**63 - 1:
                return NativeCollectionReceipt(
                    workspace_id=lease.workspace_id, actor_user_id=lease.actor_user_id,
                    membership_revision=lease.membership_revision,
                    batch_id=None, run_id=None, status="succeeded", received_update_count=0,
                    record_count=0, coverage="pending_updates_only", cursor_after=lease.cursor_before,
                ), None
            lease = await ingestion.acquire_connector_collection(
                session, source_id=source.id, source_generation=lease.source_generation,
                connector_revision=lease.connector_revision, collector_token=collector_token,
                multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            )
            if lease.configuration_revision != access_fence.configuration_revision:
                raise HTTPException(status_code=409, detail="Original Telegram access changed")
            active_lease[0] = lease
            offset = cursor.last_update_id + 1
            continue
        proof_by_id = {proof.update_id: proof for proof in classification.delivery_proofs}
        records = [
            record for delivery in page.deliveries if delivery.update_id not in replay_ids
            if (record := map_telegram_update(
                delivery.update, allowed_chat_ids=frozenset(cast("list[str]", source.configuration["telegram_chat_ids"])),
                proof=proof_by_id[delivery.update_id], collected_at=page.collected_at,
            )) is not None
        ]
        cursor_after = (
            classification.cursor_after.model_dump_json()
            if classification.cursor_after is not None else lease.cursor_before
        )
        native_batch = NativeCollectionBatch(
            source_id=source.id, source_generation=lease.source_generation,
            connector_revision=lease.connector_revision, lease_token=lease.token,
            cursor_before=lease.cursor_before, cursor_after=cursor_after, records=records,
            telegram_deliveries=classification.delivery_proofs,
            telegram_raw_deliveries=page.deliveries, coverage="pending_updates_only",
            collected_at=page.collected_at,
        )
        await _native_send_fence(
            session, source, lease, collector_token, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
            source_fence=source_fence, native_snapshot=snapshot,
        )
        last_receipt = await ingestion.accept_native_collection(
            session, native_batch, collector_token=collector_token, lease=lease,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
            expected_native_operation_id=snapshot.operation_id,
            expected_world_credential_operation_id=None,
        )
        if records or not page.deliveries or page_number == 4 or total_updates >= 500:
            return last_receipt, None
        cursor = classification.cursor_after
        if cursor is None or cursor.last_update_id >= 2**63 - 1:
            return last_receipt, None
        lease = await ingestion.acquire_connector_collection(
            session, source_id=source.id, source_generation=lease.source_generation,
            connector_revision=lease.connector_revision, collector_token=collector_token,
            multi_workspace_enabled=multi_workspace_enabled, scope=scope,
        )
        if lease.configuration_revision != access_fence.configuration_revision:
            raise HTTPException(status_code=409, detail="Original Telegram access changed")
        active_lease[0] = lease
        offset = cursor.last_update_id + 1
    if last_receipt is None:
        raise HTTPException(status_code=503, detail="Telegram collection did not produce a receipt")
    return last_receipt, None


@router.put("/{source_id}/configuration", response_model=ConnectorState,
            dependencies=[Depends(module_dependency("connectors"))])
async def configure_source(
    source_id: UUID, payload: ConnectorConfigurationRequest, session: Session, request: Request, _owner: OwnerWrite
) -> ConnectorState:
    """Validate and persist generic RSS/Web/REST settings; named native sources use revisioned provider settings.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
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
    """Owner acknowledges provider terms; clears a terms action-required gate at the new revision."""
    await _owner_access(session, request, _owner)
    await session.rollback()
    result = await provider_terms.acknowledge_terms(
        session, source_id, payload, scope=_owner,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
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


@router.post("/{source_id}/collect", response_model=ManualSyncResult, status_code=202,
             dependencies=[Depends(module_dependency("connectors"))])
async def trigger_collection(
    source_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ManualSyncResult:
    """Request collection in the selected owner workspace under its captured applied revision.

    Packaged n8n wake uses explicit original scope. MCP remains unavailable pending its
    scoped ToolPrincipal driver; no owner1 fallback can execute. Native backend scheduling
    and managed request UUID settlement remain separate C2/C4 integration gates.

    All owner reads/writes receive an explicit admitted workspace/job scope and the
    actual rollout flag; members have no Source/configuration/credential access. Browser write admission retains CSRF/backup checks and original session/access lineage.
    """
    scope = _owner
    multi_workspace_enabled = request.app.state.settings.multi_workspace_enabled
    access_fence = await _owner_access(session, request, scope)
    source = await _source(session, source_id, multi_workspace_enabled=multi_workspace_enabled, scope=scope)
    settings = request.app.state.settings
    if source.type == mcp_collection.PROVIDER_ID:
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
        expected_revision = provisioned.desired_revision
        # Exact saved identity is retained for the future scoped MCP driver.
        # The current callee has no accepted ToolPrincipal contract.
        runtime = getattr(request.app.state, "mcp_runtime", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail="MCP runtime is unavailable")
        await session.rollback()
        raise HTTPException(status_code=503, detail="Scoped MCP collector contract is pending")
    if source.status != "active" or source.type not in {"rss", "web", "api"}:
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
        await session.rollback()
        await _owner_access(session, request, scope, expected=access_fence)
        try:
            queued = await scheduler.request_collection(
                session, scope, source_id, "manual", revision, multi_workspace_enabled=multi_workspace_enabled)
        except HTTPException as exc:
            if exc.detail == TERMS_INELIGIBLE:
                raise HTTPException(status_code=409, detail="terms_not_accepted") from exc
            raise
        return ManualSyncResult(status=queued.status, request_id=queued.request_id)
    if not is_native_provider(source.provider):
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
    if not settings.n8n_webhook_token.get_secret_value():
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
    authorization: Annotated[str | None, Header()] = None,
) -> ConnectorPreview:
    """Fetch a bounded generic RSS preview, excluding feeds owned by named native adapters.

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
    _, scope, _ = await _collector(session, source_id, authorization, multi_workspace_enabled=multi_workspace_enabled)
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


@router.post("/{source_id}/crawl", response_model=CrawlResult, status_code=202)
async def submit_crawl(
    source_id: UUID,
    payload: CrawlRequest,
    session: Session,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CrawlResult:
    """Queue a source-scoped crawl request after validating its collection fence.

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
    )
    return CrawlResult(run_id=receipt.run_id)
