"""Protected native tool catalog and owner invocation endpoints."""

from typing import Annotated, Any, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import revalidate_owner_session
from core.database import get_session
from core.realtime import commit_with_replay
from core.tools import ToolExecutionPrincipal
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings.public import module_dependency
from modules.tools.browser_public import (
    _admit,
    cancel_browser_job_in_uow,
    derive_browser_job_token,
    read_browser_result,
)
from modules.tools.browser_public import (
    _read as read_browser_job,
)
from modules.tools.models import BrowserReadJob

router = APIRouter(prefix="/api/v1/tools", tags=["tools"], dependencies=[Depends(module_dependency("tools"))])
browser_jobs_router = APIRouter(prefix="/api/v1/agent-browser-jobs", tags=["agent-browser-jobs"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


class ToolInvocation(BaseModel):
    """Carry only the registered tool identity and arguments; authorization stays server-side."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=40)
    arguments: dict[str, Any]


def _visible_tools(request: Request, workspace_id: UUID) -> list[Any]:
    """Registry is process-global; drop other workspaces' MCP descriptors."""
    runtime = getattr(request.app.state, "mcp_runtime", None)
    items = request.app.state.tool_registry.list_tools()
    if runtime is None:
        return [item for item in items if not item.name.startswith("mcp.")]
    return [item for item in items if not runtime.dispatch.hides(item.name, workspace_id)]


@router.get("")
async def list_tools(request: Request, _owner: OwnerRead, _scope: WorkspaceRead) -> dict[str, Any]:
    """Return enabled registered tool contracts to the authenticated owner."""
    return {"items": [item.model_dump(mode="json") for item in _visible_tools(request, _scope.workspace_id)]}


@router.post("/invoke")
async def invoke_tool(
    request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, Any]:
    """Invoke one owner-authorized registered tool with fresh auth and output-fence checks.

    The request body is capped at 64 KB; the authenticated owner ID and token digest are detached
    before rolling back the dependency session. Builtins receive a session factory so provider or
    model waits hold no SQL transaction. Registry callbacks revalidate that same unexpired owner
    session before dispatch; after a successful handler result it is checked again immediately
    before output. Failures return bounded messages without raw tool errors. These sequential checks
    narrow stale-session windows but cannot make authentication atomic with response transmission.
    Owner authentication and CSRF remain enforced by the write dependency.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid request length") from exc
        if declared_length < 0 or declared_length > 64_000:
            raise HTTPException(status_code=413, detail="Tool invocation exceeds the request size limit")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 64_000:
            raise HTTPException(status_code=413, detail="Tool invocation exceeds the request size limit")
        body.extend(chunk)
    try:
        payload = ToolInvocation.model_validate_json(bytes(body))
    except ValidationError as exc:
        # Do not echo malformed request content or Pydantic input values in the API response.
        raise HTTPException(status_code=422, detail="Invalid tool invocation") from exc
    registry = request.app.state.tool_registry
    name = payload.name
    allowed = frozenset(item.name for item in _visible_tools(request, scope.workspace_id))
    owner_id, owner_token_hash = owner.owner_id, owner.token_hash
    principal = ToolExecutionPrincipal(
        actor_id=f"owner:{scope.user_id}", scope=scope, is_owner=True, allowed_tools=allowed,
        source_ids=frozenset(), owner_all_sources=True, destinations=frozenset({"local"}),
        capabilities=frozenset({"source.read"}),
    )

    async def revalidate_owner(current: ToolExecutionPrincipal) -> bool:
        """Recheck the same owner session in a fresh short transaction after async tool work."""
        if current.actor_id != f"owner:{scope.user_id}" or not current.is_owner or current.scope != scope:
            return False
        try:
            async with request.app.state.session_factory() as fresh_session:
                return await revalidate_owner_session(
                    fresh_session, owner_token_hash, owner_id,
                )
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    await session.rollback()
    result = await registry.invoke_tool(
        name, payload.arguments, principal, version=payload.version,
        context={"session_factory": request.app.state.session_factory,
                 "redis": request.app.state.redis,
                 "settings": request.app.state.settings,
                 "destination_id": "local", "destination_kind": "local",
                 "principal_revalidator": revalidate_owner},
    )
    if result.success and not await revalidate_owner(principal):
        raise HTTPException(status_code=401, detail="Authentication required")
    if not result.success:
        raise HTTPException(status_code=403 if result.error_code == "forbidden" else 422,
                            detail={"code": result.error_code, "message": "Tool invocation failed"})
    return cast("dict[str, Any]", result.model_dump(mode="json"))


@browser_jobs_router.get("/{job_id}")
async def read_browser_job_route(
    job_id: UUID, request: Request, session: Session, owner: OwnerRead, scope: WorkspaceRead,
) -> dict[str, Any]:
    """Read bounded job metadata and current evidence for its original owner session only."""
    flag = request.app.state.settings.multi_workspace_enabled
    await _admit(session, scope=scope, multi_workspace_enabled=flag)
    row = await session.scalar(select(BrowserReadJob).where(
        BrowserReadJob.id == job_id, BrowserReadJob.workspace_id == scope.workspace_id,
        BrowserReadJob.owner_id == scope.user_id,
    ))
    if row is None or row.auth_session_hash != owner.token_hash:
        raise HTTPException(status_code=404, detail="Browser job not found")
    result = await read_browser_result(
        session, owner.token_hash, job_id, request.app.state.session_factory,
        scope=scope, multi_workspace_enabled=flag,
    ) if row.status == "succeeded" else None
    return {
        "job": read_browser_job(row).model_dump(mode="json"),
        "result": result.model_dump(mode="json") if result is not None else None,
    }


@browser_jobs_router.post("/{job_id}/cancel")
async def cancel_browser_job_route(
    job_id: UUID, request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
) -> dict[str, Any]:
    """Commit local erasure and cancellation intent before requesting isolated service cleanup."""
    flag = request.app.state.settings.multi_workspace_enabled
    fence = await _admit(session, scope=scope, multi_workspace_enabled=flag, lock=True)
    row = await session.scalar(select(BrowserReadJob).where(
        BrowserReadJob.id == job_id, BrowserReadJob.workspace_id == scope.workspace_id,
        BrowserReadJob.owner_id == scope.user_id,
    ).with_for_update())
    if row is None or row.auth_session_hash != owner.token_hash:
        raise HTTPException(status_code=404, detail="Browser job not found")
    operation_id = row.operation_id
    instance_id = row.service_instance_id
    claim_generation = row.claim_generation
    job = await cancel_browser_job_in_uow(session, job_id, scope=scope, multi_workspace_enabled=flag)
    await commit_with_replay(session, [], scope=scope, multi_workspace_enabled=flag, access_fence=fence)
    cleanup_acknowledged = False
    if instance_id is not None:
        settings = request.app.state.settings
        shared = settings.browser_shared_token.get_secret_value()
        job_token = derive_browser_job_token(shared, row.operation_id, row.claim_generation)
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                response = await client.post(
                    f"{str(settings.browser_service_url).rstrip('/')}/agent-reads/{job_id}/cancel",
                    json={
                        "job_id": str(job_id), "operation_id": str(operation_id),
                        "claim_generation": claim_generation,
                        "service_instance_id": instance_id, "job_token": job_token,
                    },
                    headers={
                        "Authorization": f"Bearer {shared}",
                        "X-Browser-Job-Token": job_token,
                    },
                )
                if response.status_code == 200:
                    data = response.json()
                    cleanup_acknowledged = data.get("cleaned") is True
        except (httpx.HTTPError, ValueError, TypeError):
            # The durable cancel flag and erased local evidence remain authoritative;
            # the remote guard stays active until reconciliation proves cleanup.
            pass
    return {
        "job": job.model_dump(mode="json"),
        "cleanup": "acknowledged" if cleanup_acknowledged else (
            "pending" if instance_id else "not_started"
        ),
    }
