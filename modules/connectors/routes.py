from typing import Annotated, Any
from datetime import UTC, datetime
from uuid import UUID
import hashlib

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.realtime import commit_with_replay, make_source_change
from modules.connectors.public import (
    ConnectorConfigurationRequest,
    ConnectorPreview,
    ConnectorReceipt,
    CollectionFence,
    CrawlRequest,
    CrawlResult,
    RSSRequest,
    AgentBrowserGrantPatch,
    resolve_agent_browser_scope,
    update_agent_browser_grant_in_uow,
    validate_public_url,
    save_connector_configuration,
)
from modules.connectors import mcp as mcp_collection
from modules.connectors import registry
from modules.connectors import provisioning
from modules.connectors.n8n import workflow_webhook_path
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import Receipt, ReceiveBatch
from modules.sources import public as sources
from modules.sources.schemas import ConnectorSource

router = APIRouter(prefix="/api/v1/connectors/sources", tags=["connectors"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


class ConnectorState(BaseModel):
    """Represent a connector's source, configuration, cursor, and revision state."""
    model_config = ConfigDict(extra="forbid")

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


class McpCollectionRequest(BaseModel):
    """Carry a generation-checked MCP collection configuration; it never carries commands or URLs."""
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=1)
    configuration: mcp_collection.McpCollectionConfig


@router.put("/{source_id}/mcp-collection")
async def configure_mcp_collection(
    source_id: UUID, payload: McpCollectionRequest, session: Session, _owner: OwnerWrite
) -> dict[str, Any]:
    """Save the allowlisted connection and grant calls of an active ``mcp`` source.

    Grants are reviewed on the MCP connection (purpose collection, scoped to this source);
    this route only pins which reviewed grants run with which fixed arguments. Semantic
    validity is enforced at collection time against live fences.
    """
    source = await _source(session, source_id)
    if source.type != mcp_collection.PROVIDER_ID or source.status != "active":
        raise HTTPException(status_code=409, detail="Active MCP source required")
    saved = await sources.set_connector_configuration(
        session, source_id, payload.expected_generation,
        payload.configuration.model_dump(mode="json"),
    )
    if saved is None:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Source changed while configuration was validated")
    await commit_with_replay(session, [make_source_change(saved.id, saved.generation, saved.status)])
    return {
        "source_id": saved.id, "source_generation": saved.generation,
        "configuration": saved.configuration,
    }


@router.get("/{source_id}/agent-browser-grant")
async def read_agent_browser_grant(
    source_id: UUID, session: Session, _owner: OwnerWrite
) -> dict[str, object]:
    """Return the current source-bound browser opt-in, without exposing connector credentials."""
    scope = await resolve_agent_browser_scope(session, _owner.owner_id, source_id)
    if scope is None:
        return {"available": False, "enabled": False}
    return {"available": True, **scope.__dict__}


@router.put("/{source_id}/agent-browser-grant")
async def update_agent_browser_grant(
    source_id: UUID,
    payload: AgentBrowserGrantPatch,
    expected_revision: int,
    session: Session,
    _owner: OwnerWrite,
) -> dict[str, object]:
    """Apply owner browser opt-in with optimistic grant and source configuration fences."""
    try:
        scope = await update_agent_browser_grant_in_uow(
            session, _owner.owner_id, source_id, expected_revision, payload
        )
        await session.commit()
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Active web source is unavailable") from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Browser grant revision is stale") from exc
    return {"available": True, **scope.__dict__}


async def _source(session: AsyncSession, source_id: UUID) -> ConnectorSource:
    """Load the connector source projection or raise HTTP 404."""
    source = await sources.get_connector_source(session, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


async def _collector(
    session: AsyncSession, source_id: UUID, authorization: str | None
) -> str:
    """Authenticate a bearer collector token for the requested source."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not await ingestion.collector_can_ingest(
        session, source_id, token
    ):
        raise HTTPException(status_code=401, detail="Source collector authentication required")
    return token


@router.put("/{source_id}/configuration", response_model=ConnectorState)
async def configure_source(
    source_id: UUID, payload: ConnectorConfigurationRequest, session: Session, _owner: OwnerWrite
) -> ConnectorState:
    """Validate and persist connector settings using owner-write authorization."""
    source = await _source(session, source_id)
    if source.type not in registry.SUPPORTED_TYPES:
        raise HTTPException(status_code=422, detail="This source type has no packaged connector")
    candidate = source.model_copy(
        update={"configuration": payload.configuration.model_dump(mode="json", exclude_none=True)}
    )
    try:
        data = registry.validate(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await validate_public_url(data["url"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    desired = dict(candidate.configuration)
    desired["auth_method"] = "none"
    saved_result = await save_connector_configuration(
        session,
        source,
        payload.expected_revision,
        candidate.configuration,
        desired,
    )
    if saved_result is None:
        raise HTTPException(status_code=409, detail="Source or connector revision changed while configuration was validated")
    saved, provisioning_row = saved_result
    result = registry.sync(saved, None)
    result["connector_revision"] = provisioning_row.desired_revision
    await commit_with_replay(session, [
        make_source_change(
            saved.id, saved.generation, saved.status,
            connector_state=provisioning_row.state,
        ),
    ])
    return ConnectorState(**result)


@router.post("/{source_id}/collect", response_model=ManualSyncResult, status_code=202)
async def trigger_collection(
    source_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
) -> ManualSyncResult:
    """Queue a manual collection run for an active supported connector."""
    source = await _source(session, source_id)
    settings = request.app.state.settings
    if source.type == mcp_collection.PROVIDER_ID:
        # MCP collection runs in-process through the reviewed client; no n8n workflow is involved.
        runtime = getattr(request.app.state, "mcp_runtime", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail="MCP runtime is unavailable")
        await session.rollback()
        try:
            return ManualSyncResult.model_validate(await mcp_collection.collect(
                runtime, request.app.state.session_factory, source_id,
            ))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Source not found") from exc
        except mcp_collection.McpCollectionError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
    if source.status != "active" or source.type not in {"rss", "web", "api"}:
        raise HTTPException(status_code=409, detail="Active packaged connector required")
    provisioned = await provisioning.activation_status(session, source_id)
    if (
        provisioned is None
        or provisioned.state != "active"
        or provisioned.applied_revision != provisioned.desired_revision
        or provisioned.source_generation != source.generation
    ):
        raise HTTPException(status_code=409, detail="Enable this source from connector settings before collecting")
    if not await provisioning.require_collection_fence(
        session, source, source.generation, provisioned.desired_revision, lock=True
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await session.rollback()
    token = settings.n8n_webhook_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=503, detail="Manual n8n trigger authentication is not configured")
    try:
        data = registry.validate(source)
        await validate_public_url(data["url"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail="Configure and validate the connector before syncing") from exc
    try:
        async with httpx.AsyncClient(timeout=75, trust_env=False) as client:
            response = await client.post(
                f"{str(settings.n8n_service_url).rstrip('/')}/webhook/{workflow_webhook_path(source_id, source.type)}",
                json={
                    "source_id": str(source_id),
                    "source_generation": source.generation,
                    "connector_revision": provisioned.desired_revision,
                },
                headers={"X-BBD-Webhook-Token": token},
            )
            response.raise_for_status()
        return ManualSyncResult.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        if await sources.record_collection_result(
            session, source_id, source.generation, datetime.now(UTC), "n8n_unavailable"
        ):
            current = await sources.lock_source(session, source_id)
            drafts = [
                make_source_change(current.id, current.generation, current.status)
            ] if current is not None else []
            await commit_with_replay(session, drafts)
        raise HTTPException(status_code=503, detail="n8n collection workflow is unavailable or failed") from exc


@router.post("/{source_id}/validate", response_model=ConnectorState)
async def validate_source(
    source_id: UUID,
    session: Session,
    payload: CollectionFence,
    authorization: Annotated[str | None, Header()] = None,
) -> ConnectorState:
    """Validate an authorized source's connector configuration and URL policy."""
    await _collector(session, source_id, authorization)
    source = await _source(session, source_id)
    if not await provisioning.require_validation_fence(
        session, source, payload.source_generation, payload.connector_revision
    ):
        raise HTTPException(status_code=409, detail="Connector validation fence is stale")
    await _collector(session, source_id, authorization)
    await session.rollback()
    source = await _source(session, source_id)
    try:
        data = registry.validate(source)
        await validate_public_url(data["url"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    cursor = await ingestion.get_source_cursor(session, source_id)
    current = await _source(session, source_id)
    if not await provisioning.require_validation_fence(
        session, current, payload.source_generation, payload.connector_revision
    ):
        raise HTTPException(status_code=409, detail="Connector validation fence changed during validation")
    await _collector(session, source_id, authorization)
    result = registry.sync(source, cursor)
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
    """Fetch and return a bounded RSS preview for an authorized source."""
    await _collector(session, source_id, authorization)
    source = await _source(session, source_id)
    if not await provisioning.require_collection_fence(
        session, source, source_generation, connector_revision, lock=True
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector(session, source_id, authorization)
    await session.rollback()
    source = await _source(session, source_id)
    try:
        data = registry.validate(source)
        if source.type != "rss":
            raise ValueError("RSS/Atom source required")
        await validate_public_url(data["url"])
        cursor = await ingestion.get_source_cursor(session, source_id)
        settings = request.app.state.settings
        token = settings.browser_shared_token.get_secret_value()
        if not token:
            raise HTTPException(status_code=503, detail="Browser collector is not configured")
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
        current = await _source(session, source_id)
        if not await provisioning.require_collection_fence(
            session, current, source_generation, connector_revision, lock=True
        ):
            raise HTTPException(status_code=409, detail="Connector collection fence changed during collection")
        await _collector(session, source_id, authorization)
        await session.rollback()
        return ConnectorPreview.model_validate(result)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=422, detail="RSS source could not be collected") from exc


@router.post("/{source_id}/sync", response_model=Receipt, status_code=202)
async def receive_connector_batch(
    source_id: UUID,
    payload: ConnectorReceipt,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> Receipt:
    """Accept a connector batch after bearer token and source fencing checks."""
    collector_token = await _collector(session, source_id, authorization)
    source = await _source(session, source_id)
    if not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector(session, source_id, authorization)
    try:
        registry.validate(source)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    batch = ReceiveBatch(
        source_id=source_id,
        source_generation=payload.source_generation,
        connector_revision=payload.connector_revision,
        batch_key="connector:" + hashlib.sha256(
            payload.model_dump_json().encode()
        ).hexdigest(),
        cursor_before=payload.cursor_before,
        cursor_after=payload.cursor_after,
        records=[record.model_dump() for record in payload.records],
    )
    return await ingestion.receive_connector_batch(session, batch, collector_token)


@router.post("/{source_id}/no-changes", response_model=ManualSyncResult)
async def acknowledge_no_changes(
    source_id: UUID,
    payload: CollectionFence,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> ManualSyncResult:
    """Record a successful collection that did not produce new observations."""
    await _collector(session, source_id, authorization)
    source = await _source(session, source_id)
    if not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True
    ):
        raise HTTPException(status_code=409, detail="Connector collection fence is stale")
    await _collector(session, source_id, authorization)
    now = datetime.now(UTC)
    if not await sources.record_collection_result(
        session, source_id, payload.source_generation, now, None, no_changes=True
    ):
        raise HTTPException(status_code=409, detail="Source is no longer active")
    current = await sources.lock_source(session, source_id)
    drafts = [
        make_source_change(current.id, current.generation, current.status)
    ] if current is not None else []
    await commit_with_replay(session, drafts)
    return ManualSyncResult(status="no_changes")


@router.post("/{source_id}/crawl", response_model=CrawlResult, status_code=202)
async def submit_crawl(
    source_id: UUID,
    payload: CrawlRequest,
    session: Session,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CrawlResult:
    """Queue a source-scoped crawl request after validating its collection fence."""
    await _collector(session, source_id, authorization)
    settings = request.app.state.settings
    source = await _source(session, source_id)
    if source.type != "web" or not await provisioning.require_collection_fence(
        session, source, payload.source_generation, payload.connector_revision, lock=True
    ):
        raise HTTPException(status_code=409, detail="Active web source required")
    await _collector(session, source_id, authorization)
    try:
        config = registry.configuration(source)
        if config.url is None:
            raise ValueError("Web connector URL is required")
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
    cursor = await ingestion.get_source_cursor(session, source_id)
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
    )
    return CrawlResult(run_id=receipt.run_id)
