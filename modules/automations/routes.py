"""Protected REST routes for automation rules and dry preview."""

import asyncio
import json
import re
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession, Owner
from core.database import get_session
from core.realtime import commit_with_replay
from core.workspaces import public as workspaces
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.automations import public
from modules.automations.execution import enqueue_trigger
from modules.automations.models import AutomationTrigger, AutomationWebhookCredential
from modules.automations.schemas import (
    AutomationCreate,
    AutomationPage,
    AutomationRead,
    AutomationUpdate,
    CapabilitiesRead,
    DecisionRequest,
    ManualRunRequest,
    PreviewRequest,
    PreviewResult,
    RunPage,
    RunRead,
)
from modules.automations.scope import _actor, _admit, _require_owner
from modules.settings.public import module_dependency, module_is_enabled, register_request_activity

router = APIRouter(prefix="/api/v1/automations", tags=["automations"], dependencies=[Depends(module_dependency("automations"))])
webhook_router = APIRouter(prefix="/api/v1/automations", tags=["automation-webhooks"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
WEBHOOK_TOKEN_TTL = timedelta(days=90)
WEBHOOK_BODY_LIMIT = 64 * 1024
_EVENT_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


class InboundEvent(BaseModel):
    """Allow only the declared metadata field accepted by webhook trigger conditions."""

    model_config = ConfigDict(extra="forbid")
    event: str = Field(min_length=1, max_length=2000)


def _credential_stmt(alias: str, token_digest: str) -> Select[Any]:
    """Live (not revoked, not expired) credential for an alias and token digest; the digest is the selector."""
    return select(AutomationWebhookCredential).where(
        AutomationWebhookCredential.alias == alias,
        AutomationWebhookCredential.token_hash == token_digest,
        AutomationWebhookCredential.revoked_at.is_(None),
        AutomationWebhookCredential.expires_at > datetime.now(UTC),
    ).limit(2)


def _error(code: int, name: str, message: str, details: dict[str, Any] | None = None) -> HTTPException:
    """Build the standard error envelope without echoing rule content."""
    return HTTPException(status_code=code, detail={"code": name, "message": message, "details": details or {}})


def _flag(request: Request) -> bool:
    """The configured multi-workspace flag; never defaulted."""
    flag: bool = request.app.state.settings.multi_workspace_enabled
    return flag


async def _call[T](operation: Awaitable[T]) -> T:
    """Await a service call and map domain exceptions to HTTP errors."""
    try:
        return await operation
    except public.AutomationMissing as exc:
        raise _error(404, "automation_not_found", "Automation not found") from exc
    except public.AutomationConflict as exc:
        details = {"current_revision": exc.current_revision} if exc.current_revision is not None else {}
        raise _error(409, exc.code, str(exc), details) from exc
    except public.PauseBeforeBriefEdit as exc:
        raise _error(422, "pause_before_brief_edit", str(exc)) from exc
    except public.AutomationInvalid as exc:
        raise _error(422, "invalid_automation", str(exc)) from exc
    except public.RunMissing as exc:
        raise _error(404, "run_not_found", "Automation or run not found") from exc
    except public.RunConflict as exc:
        details = {"current_revision": exc.current_revision} if exc.current_revision is not None else {}
        raise _error(409, exc.code, str(exc), details) from exc


@router.get("", response_model=AutomationPage)
async def list_automations(
    request: Request, session: Session, owner: OwnerRead, scope: WorkspaceRead, response: Response,
    enabled: bool | None = None, trigger_type: Annotated[str | None, Query(max_length=32)] = None,
) -> AutomationPage:
    """List the owner's live rules; never cacheable."""
    response.headers["Cache-Control"] = "private, no-store"
    return await public.list_automations(
        session, enabled=enabled, trigger_type=trigger_type, scope=scope, multi_workspace_enabled=_flag(request))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AutomationRead)
async def create_automation(
    payload: AutomationCreate, request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
    response: Response,
) -> AutomationRead:
    """Create a rule at revision 1 after CSRF-protected owner auth and dependency/allowlist checks."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.create_automation(
        session, payload, request.app.state.modules, request.app.state.settings,
        scope=scope, multi_workspace_enabled=_flag(request)))


@router.get("/capabilities", response_model=CapabilitiesRead)
async def get_capabilities(
    request: Request, owner: OwnerRead, scope: WorkspaceRead, response: Response,
) -> CapabilitiesRead:
    """Return editor options: trigger fields, action availability by owning module and webhook alias names."""
    response.headers["Cache-Control"] = "private, no-store"
    _require_owner(scope)
    return public.capabilities(request.app.state.modules, request.app.state.settings)


@router.post("/webhook-credentials/{alias}")
async def issue_webhook_credential(
    alias: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,39}$")],
    request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite, response: Response,
) -> dict[str, object]:
    """Rotate one alias token and show its random bearer exactly once to the authenticated owner."""
    response.headers["Cache-Control"] = "private, no-store"
    from modules.tools.mcp_credentials import issue_inbound_token

    flag = _flag(request)
    fence = await _admit(session, scope=scope, multi_workspace_enabled=flag, lock=True)
    raw, digest, _prefix = issue_inbound_token()
    await session.scalar(select(Owner).where(Owner.id == owner.owner_id).with_for_update())
    row = await session.scalar(select(AutomationWebhookCredential).where(
        AutomationWebhookCredential.workspace_id == scope.workspace_id,
        AutomationWebhookCredential.owner_id == _actor(scope),
        AutomationWebhookCredential.alias == alias,
    ).with_for_update())
    if row is None:
        row = AutomationWebhookCredential(
            workspace_id=scope.workspace_id, owner_id=_actor(scope), alias=alias, token_hash=digest, revision=1,
            expires_at=datetime.now(UTC) + WEBHOOK_TOKEN_TTL,
        )
        session.add(row)
    else:
        row.token_hash = digest
        row.revision += 1
        row.created_at = datetime.now(UTC)
        row.expires_at = datetime.now(UTC) + WEBHOOK_TOKEN_TTL
        row.revoked_at = None
    await commit_with_replay(session, [], scope=scope, multi_workspace_enabled=flag, access_fence=fence)
    return {
        "alias": alias, "token": raw, "revision": row.revision,
        "expires_at": row.expires_at, "endpoint": f"/api/v1/automations/inbound/{alias}",
    }


@router.delete("/webhook-credentials/{alias}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_webhook_credential(
    alias: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,39}$")],
    request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
) -> Response:
    """Immediately revoke one alias credential while retaining trigger and run history."""
    flag = _flag(request)
    fence = await _admit(session, scope=scope, multi_workspace_enabled=flag, lock=True)
    row = await session.scalar(select(AutomationWebhookCredential).where(
        AutomationWebhookCredential.workspace_id == scope.workspace_id,
        AutomationWebhookCredential.owner_id == _actor(scope),
        AutomationWebhookCredential.alias == alias,
    ).with_for_update())
    if row is not None:
        row.revoked_at = datetime.now(UTC)
        await commit_with_replay(session, [], scope=scope, multi_workspace_enabled=flag, access_fence=fence)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@webhook_router.post("/inbound/{alias}", status_code=status.HTTP_202_ACCEPTED)
async def receive_inbound_webhook(
    alias: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,39}$")],
    request: Request, session: Session, response: Response,
    token: Annotated[str | None, Header(alias="X-Umwelt-Webhook-Token")] = None,
    event_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    """Authenticate a configured alias, validate bounded metadata, then commit its durable dedupe inbox row.

    This endpoint uses only the independent per-alias bearer, never the owner's browser session.
    It reads persisted automation availability only after bearer validation. The durable inbox
    unique key absorbs provider retries; request bodies and tokens are not logged.
    """
    response.headers["Cache-Control"] = "no-store"
    if not token or not event_key or not _EVENT_KEY.fullmatch(event_key):
        raise HTTPException(status_code=401, detail="Webhook credentials are required")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Webhook requires application/json")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise HTTPException(status_code=400, detail="Webhook content length is invalid")
            if declared_length > WEBHOOK_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="Webhook body exceeds 64 KiB")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Webhook content length is invalid") from exc
    from modules.tools.mcp_credentials import hash_inbound_token, verify_inbound_token

    flag = _flag(request)
    # The URL carries no workspace: alias plus the token digest is the unique selector (Recipe W).
    try:
        digest = hash_inbound_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Webhook credentials are invalid or expired") from exc
    matches = (await session.scalars(_credential_stmt(alias, digest))).all()
    if len(matches) != 1 or not verify_inbound_token(token, matches[0].token_hash):
        raise HTTPException(status_code=401, detail="Webhook credentials are invalid or expired")
    credential = matches[0]
    owner = await workspaces.resolve_workspace_owner_context(
        session, credential.workspace_id, multi_workspace_enabled=flag)
    if owner is None or owner.user_id != credential.owner_id:
        raise HTTPException(status_code=401, detail="Webhook credentials are invalid or expired")
    scope = InternalJobScope(
        workspace_id=credential.workspace_id, actor_user_id=owner.user_id, membership_revision=owner.membership_revision)
    # External trigger ingress uses its own bearer. Check persisted availability only after it
    # authenticates, since this router deliberately has no owner-session/CSRF dependency.
    if not await module_is_enabled(session, "automations", scope=scope, multi_workspace_enabled=flag):
        raise HTTPException(status_code=404, detail="Automation webhooks are unavailable")
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > WEBHOOK_BODY_LIMIT:
                    raise HTTPException(status_code=413, detail="Webhook body exceeds 64 KiB")
                body.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Webhook ingress timed out") from exc
    try:
        payload = InboundEvent.model_validate(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Webhook event payload is invalid") from exc
    # Commit the global admission before taking the credential row lock used for the durable inbox write.
    await register_request_activity(request, session, "automation_webhook_ingress", alias)
    fence = await _admit(session, scope=scope, multi_workspace_enabled=flag, lock=True)
    credential = await session.scalar(_credential_stmt(alias, digest).where(
        AutomationWebhookCredential.workspace_id == scope.workspace_id,
        AutomationWebhookCredential.owner_id == _actor(scope),
    ).with_for_update().execution_options(populate_existing=True))
    if credential is None or not verify_inbound_token(token, credential.token_hash):
        raise HTTPException(status_code=401, detail="Webhook credentials are invalid or expired")
    if not await module_is_enabled(session, "automations", scope=scope, multi_workspace_enabled=flag):
        raise HTTPException(status_code=404, detail="Automation webhooks are unavailable")
    accepted = await enqueue_trigger(
        session, "webhook", f"{alias}:{event_key}",
        {"event": payload.event}, hook=alias, scope=scope, multi_workspace_enabled=flag,
    )
    if not accepted:
        prior = await session.scalar(select(AutomationTrigger).where(
            AutomationTrigger.workspace_id == scope.workspace_id,
            AutomationTrigger.owner_id == _actor(scope),
            AutomationTrigger.trigger_type == "webhook",
            AutomationTrigger.event_key == f"{alias}:{event_key}",
        ))
        if prior is None or prior.payload != {"event": payload.event, "hook": alias}:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another event")
    await commit_with_replay(session, [], scope=scope, multi_workspace_enabled=flag, access_fence=fence)
    return {"accepted": accepted}


# Declared before ``/{automation_id}`` so "preview" is never parsed as an ID.
@router.post("/preview", response_model=PreviewResult)
async def preview_automation(
    payload: PreviewRequest, request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceRead,
    response: Response,
) -> PreviewResult:
    """Dry-run a definition or stored rule against a sample; queues nothing and calls no model."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.preview(session, payload, scope=scope, multi_workspace_enabled=_flag(request)))


@router.get("/{automation_id}", response_model=AutomationRead)
async def get_automation(
    automation_id: UUID, request: Request, session: Session, owner: OwnerRead, scope: WorkspaceRead,
    response: Response,
) -> AutomationRead:
    """Return one rule at its current revision."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.get_automation(
        session, automation_id, scope=scope, multi_workspace_enabled=_flag(request)))


@router.patch("/{automation_id}", response_model=AutomationRead)
async def update_automation(
    automation_id: UUID, payload: AutomationUpdate, request: Request, session: Session,
    owner: OwnerWrite, scope: WorkspaceWrite, response: Response,
) -> AutomationRead:
    """Append a new immutable revision from a revision-fenced patch."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.update_automation(
        session, automation_id, payload, request.app.state.modules, request.app.state.settings,
        scope=scope, multi_workspace_enabled=_flag(request)))


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: UUID, request: Request, session: Session, owner: OwnerWrite, scope: WorkspaceWrite,
    expected_revision: Annotated[int, Query(ge=1)],
) -> Response:
    """Soft-delete a rule (history retained) when the expected revision matches."""
    await _call(public.delete_automation(
        session, automation_id, expected_revision, scope=scope, multi_workspace_enabled=_flag(request)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{automation_id}/conversation")
async def get_automation_conversation(
    automation_id: UUID, request: Request, session: Session, owner: OwnerRead, scope: WorkspaceRead,
    response: Response,
) -> dict[str, UUID | None]:
    """Return the per-rule Chat conversation id so run detail can open it; null until an agent action ran."""
    response.headers["Cache-Control"] = "private, no-store"
    return {"conversation_id": await _call(public.get_automation_conversation_id(
        session, automation_id, scope=scope, multi_workspace_enabled=_flag(request)))}


@router.post("/{automation_id}/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunRead)
async def run_automation(
    automation_id: UUID, payload: ManualRunRequest, request: Request, session: Session, owner: OwnerWrite,
    scope: WorkspaceWrite, response: Response,
) -> RunRead:
    """Queue one run now; retries with the same client_request_id return the same run.

    The run is executed by the worker and every approval-gated action still stops for approval.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.start_manual(
        session, automation_id, payload.expected_revision, payload.client_request_id,
        scope=scope, multi_workspace_enabled=_flag(request)))


@router.get("/{automation_id}/runs", response_model=RunPage)
async def list_automation_runs(
    automation_id: UUID, request: Request, session: Session, owner: OwnerRead, scope: WorkspaceRead,
    response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RunPage:
    """Newest-first run history with per-action outcomes (codes only), kept after pause or delete."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.list_runs(
        session, automation_id, limit, scope=scope, multi_workspace_enabled=_flag(request)))


@router.post("/runs/{run_id}/actions/{ordinal}/decision", response_model=RunRead)
async def decide_run_action(
    run_id: UUID, ordinal: Annotated[int, Path(ge=1, le=10)], payload: DecisionRequest, request: Request,
    session: Session, owner: OwnerWrite, scope: WorkspaceWrite, response: Response,
) -> RunRead:
    """Approve or deny an action waiting for approval; approval is bound to the cited revision."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.decide_action(
        session, owner.token_hash, run_id, ordinal, payload.decision == "approve",
        request.app.state.settings, scope=scope, multi_workspace_enabled=_flag(request)))
