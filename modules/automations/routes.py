"""Protected REST routes for automation rules and dry preview."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.automations import public
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

router = APIRouter(prefix="/api/v1/automations", tags=["automations"])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


def _error(code: int, name: str, message: str, details: dict | None = None) -> HTTPException:
    """Build the standard error envelope without echoing rule content."""
    return HTTPException(status_code=code, detail={"code": name, "message": message, "details": details or {}})


async def _call(operation):
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
    session: Session, owner: OwnerRead, response: Response, enabled: bool | None = None,
    trigger_type: Annotated[str | None, Query(max_length=32)] = None,
) -> AutomationPage:
    """List the owner's live rules; never cacheable."""
    response.headers["Cache-Control"] = "private, no-store"
    return await public.list_automations(session, owner.owner_id, enabled=enabled, trigger_type=trigger_type)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AutomationRead)
async def create_automation(
    payload: AutomationCreate, request: Request, session: Session, owner: OwnerWrite, response: Response,
) -> AutomationRead:
    """Create a rule at revision 1 after CSRF-protected owner auth and dependency/allowlist checks."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.create_automation(
        session, owner.owner_id, payload, request.app.state.modules, request.app.state.settings))


@router.get("/capabilities", response_model=CapabilitiesRead)
async def get_capabilities(request: Request, owner: OwnerRead, response: Response) -> CapabilitiesRead:
    """Return editor options: trigger fields, action availability by owning module and webhook alias names."""
    response.headers["Cache-Control"] = "private, no-store"
    return public.capabilities(request.app.state.modules, request.app.state.settings)


# Declared before ``/{automation_id}`` so "preview" is never parsed as an ID.
@router.post("/preview", response_model=PreviewResult)
async def preview_automation(
    payload: PreviewRequest, session: Session, owner: OwnerWrite, response: Response,
) -> PreviewResult:
    """Dry-run a definition or stored rule against a sample; queues nothing and calls no model."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.preview(session, owner.owner_id, payload))


@router.get("/{automation_id}", response_model=AutomationRead)
async def get_automation(automation_id: UUID, session: Session, owner: OwnerRead, response: Response) -> AutomationRead:
    """Return one rule at its current revision."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.get_automation(session, owner.owner_id, automation_id))


@router.patch("/{automation_id}", response_model=AutomationRead)
async def update_automation(
    automation_id: UUID, payload: AutomationUpdate, request: Request, session: Session,
    owner: OwnerWrite, response: Response,
) -> AutomationRead:
    """Append a new immutable revision from a revision-fenced patch."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.update_automation(
        session, owner.owner_id, automation_id, payload, request.app.state.modules, request.app.state.settings))


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: UUID, session: Session, owner: OwnerWrite,
    expected_revision: Annotated[int, Query(ge=1)],
) -> Response:
    """Soft-delete a rule (history retained) when the expected revision matches."""
    await _call(public.delete_automation(session, owner.owner_id, automation_id, expected_revision))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{automation_id}/conversation")
async def get_automation_conversation(
    automation_id: UUID, session: Session, owner: OwnerRead, response: Response,
) -> dict[str, UUID | None]:
    """Return the per-rule Chat conversation id so run detail can open it; null until an agent action ran."""
    response.headers["Cache-Control"] = "private, no-store"
    return {"conversation_id": await _call(public.get_automation_conversation_id(session, owner.owner_id, automation_id))}


@router.post("/{automation_id}/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunRead)
async def run_automation(
    automation_id: UUID, payload: ManualRunRequest, session: Session, owner: OwnerWrite, response: Response,
) -> RunRead:
    """Queue one run now; retries with the same client_request_id return the same run.

    The run is executed by the worker and every approval-gated action still stops for approval.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.start_manual(
        session, owner.owner_id, automation_id, payload.expected_revision, payload.client_request_id))


@router.get("/{automation_id}/runs", response_model=RunPage)
async def list_automation_runs(
    automation_id: UUID, session: Session, owner: OwnerRead, response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RunPage:
    """Newest-first run history with per-action outcomes (codes only), kept after pause or delete."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.list_runs(session, owner.owner_id, automation_id, limit))


@router.post("/runs/{run_id}/actions/{ordinal}/decision", response_model=RunRead)
async def decide_run_action(
    run_id: UUID, ordinal: Annotated[int, Path(ge=1, le=10)], payload: DecisionRequest, request: Request,
    session: Session, owner: OwnerWrite, response: Response,
) -> RunRead:
    """Approve or deny an action waiting for approval; approval is bound to the cited revision."""
    response.headers["Cache-Control"] = "private, no-store"
    return await _call(public.decide_action(
        session, owner.owner_id, owner.token_hash, run_id, ordinal, payload.decision == "approve",
        request.app.state.settings))
