"""Owner-only configuration for ephemeral live-map provider access."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import is_instance_operator, require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.publication import require_publication_gate
from core.workspaces.dependencies import require_workspace_observer_read, require_workspace_write
from core.workspaces.public import (
    bump_operator_review_revision,
    list_instance_workspaces,
    read_access_fence,
)
from core.workspaces.schemas import AdminWorkspaceReadContext, PublicationFence, WorkspaceContext
from modules.dashboard.live_credentials import (
    LiveCredentialUpdate,
    LiveMapTerms,
    public_live_credentials,
    read_live_credentials,
    save_live_credentials,
)

router = APIRouter(prefix="/api/v1/live-map", tags=["live-map"])
operator_router = APIRouter(prefix="/api/v1/operator/live-map", tags=["live-map"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext | AdminWorkspaceReadContext, Depends(require_workspace_observer_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@router.get("/config")
async def read_config(request: Request, response: Response, session: Session, scope: OwnerRead) -> dict[str, Any]:
    """Return sanitized settings to the owner or operator viewer; never expose saved keys."""
    response.headers["Cache-Control"] = "private, no-store"
    operator = is_instance_operator(request.state.auth_session)
    gate = request.app.state.settings.multi_workspace_enabled
    fence = await read_access_fence(session, scope=scope, multi_workspace_enabled=gate)
    value = await read_live_credentials(session, scope=scope, settings=request.app.state.settings,
                                        multi_workspace_enabled=gate)
    await session.rollback()
    require_publication_gate(request, PublicationFence(scope=scope, access_fence=fence,
                             auth_session=authenticated_session_ref(request), grants=()))
    result = public_live_credentials(value, is_operator=operator)
    if isinstance(scope, AdminWorkspaceReadContext):
        # Contact information belongs to the workspace owner, not the operator viewer.
        result["sec_contact_email"] = None
    return result


@router.put("/config")
async def save_config(value: LiveCredentialUpdate, request: Request, response: Response,
                      session: Session, scope: OwnerWrite) -> dict[str, Any]:
    """Commit an owner/CSRF-protected settings revision without contacting providers."""
    response.headers["Cache-Control"] = "private, no-store"
    operator = is_instance_operator(request.state.auth_session)
    saved = await save_live_credentials(session, value, scope=scope, settings=request.app.state.settings,
                                       multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
                                       auth_sessions=(authenticated_session_ref(request),),
                                       is_operator=operator)
    await session.commit()
    fence = await read_access_fence(session, scope=scope,
                                    multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    current = await read_live_credentials(session, scope=scope, settings=request.app.state.settings,
                                          multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if current.revision != saved.revision:
        raise HTTPException(status_code=409, detail="Live map settings changed")
    await session.rollback()
    require_publication_gate(request, PublicationFence(scope=scope, access_fence=fence,
                             auth_session=authenticated_session_ref(request), grants=()))
    return public_live_credentials(saved, is_operator=operator)


class LiveTermsDecision(BaseModel):
    """Decision bound to the exact owner acknowledgement seen by the operator."""

    model_config = ConfigDict(extra="forbid")
    state: Literal["approved", "rejected"]
    evidence_ref: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=64)
    declared_use: Literal["personal", "noncommercial", "commercial"]
    acknowledged_at: datetime


@operator_router.get("/terms-review")
async def pending_live_terms(session: Session, operator: Annotated[AuthSession, Depends(require_owner)]) -> list[dict[str, Any]]:
    """List only pending live terms metadata; never return credentials or evidence."""
    from modules.connectors.public import live_terms_catalog

    del operator
    names = {workspace.id: workspace.name for workspace in await list_instance_workspaces(session)}
    rows = await session.scalars(select(LiveMapTerms).where(LiveMapTerms.review_state == "pending"))
    catalog = live_terms_catalog()
    return [{"workspace_id": str(row.workspace_id), "workspace_name": names[row.workspace_id],
             "provider_id": row.provider_id, "terms_url": catalog[row.provider_id]["terms_url"],
             "terms_version": row.version, "declared_use": row.declared_use,
             "acknowledged_at": row.acknowledged_at.isoformat()}
            for row in rows if row.workspace_id in names and row.provider_id in catalog
            and catalog[row.provider_id]["eligibility"] == "review"
            and catalog[row.provider_id]["version"] == row.version]


@operator_router.put("/{workspace_id}/{provider_id}/terms-review")
async def review_live_terms(workspace_id: UUID, provider_id: str, value: LiveTermsDecision,
                            request: Request, session: Session,
                            operator: Annotated[AuthSession, Depends(require_owner_write)]) -> dict[str, str]:
    """Record an operator decision without granting workspace or credential access.

    The workspace fence is locked before the terms row; stale acknowledgements fail closed.
    """
    from modules.connectors.public import live_terms_catalog

    del operator
    item = live_terms_catalog().get(provider_id)
    if not item or item["eligibility"] != "review" or not value.evidence_ref.strip():
        raise HTTPException(status_code=422, detail="Invalid review")
    await bump_operator_review_revision(session, workspace_id=workspace_id,
                                        auth_session=authenticated_session_ref(request))
    row = await session.scalar(select(LiveMapTerms).where(
        LiveMapTerms.workspace_id == workspace_id, LiveMapTerms.provider_id == provider_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (row is None or row.review_state != "pending" or row.version != value.version
            or item["version"] != value.version or row.declared_use != value.declared_use
            or row.acknowledged_at != value.acknowledged_at):
        raise HTTPException(status_code=409, detail="Live terms acknowledgement changed")
    row.review_state, row.reviewer_user_id = value.state, 1
    row.reviewed_at, row.evidence_ref = datetime.now(UTC), value.evidence_ref.strip()
    await session.commit()
    return {"state": value.state}
