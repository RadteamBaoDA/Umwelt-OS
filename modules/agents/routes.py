"""Owner-authenticated endpoints for legacy assistant runs and versioned specialist profiles."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.agents import public
from modules.agents.schemas import (
    AgentProfilePatch, AgentProfileRead, AgentRunPage, ProfileRunStart,
    ApprovalDecisionRead, ApprovalDecisionRequest, ApprovalRead, AgentRunRead, AgentRunStart,
)
from modules.agents.approvals import decision as resolve_decision
from modules.agents.internal_writes import INTERNAL_DESTINATION, INTERNAL_WRITE_TOOLS
from modules.tools.webhook import load_webhook_profiles
from modules.agents.specialists import get_profile, list_profiles, update_profile_in_uow
from modules.settings import public as settings_public
from modules.settings.public import module_dependency

router = APIRouter(tags=["agents"], dependencies=[Depends(module_dependency("agents"))])
logger = logging.getLogger(__name__)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.post("/api/v1/agents/{agent_id}/runs", response_model=AgentRunRead, status_code=202)
async def start_run(
    agent_id: str, payload: AgentRunStart | ProfileRunStart, request: Request, session: Session, owner: OwnerWrite,
) -> AgentRunRead:
    """Persist a bounded assistant run after owner/CSRF checks and return its identifier.

    The assistant payload retains its legacy contract; profile payloads pin exact profile and
    gateway snapshots and require a live Chat link. PostgreSQL commits first; Redis dispatch is
    best-effort because the existing worker reconciler retries durable queued rows after failures.
    """
    if len(payload.prompt.encode("utf-8")) > 32_000:
        raise HTTPException(status_code=413, detail="Agent prompt exceeds the size limit")
    if agent_id == "assistant" and isinstance(payload, AgentRunStart):
        return await public.create_run(session, owner.token_hash, payload, request.app.state.tool_registry)
    if agent_id == "assistant" or not isinstance(payload, ProfileRunStart):
        raise HTTPException(status_code=422, detail="Profile run request is invalid")
    config = await settings_public.get_ai_execution_config(
        session, request.app.state.settings, request.app.state.redis,
    )
    result = await public.create_profile_run_in_uow(
        session, owner.owner_id, owner.token_hash, agent_id, payload,
        request.app.state.tool_registry, config,
    )
    await session.commit()
    # PostgreSQL is the durable queue; a lost Redis push is replayed by the existing reconciler.
    try:
        await request.app.state.redis.enqueue_job(
            "process_agent_run", str(result.id), 1,
            _job_id=f"agent-run:{result.id}:1",
        )
    except Exception as exc:
        logger.warning("Agent run dispatch deferred for %s (%s)", result.id, type(exc).__name__)
    return result


@router.get("/api/v1/agents/profiles", response_model=list[AgentProfileRead])
async def read_profiles(request: Request, session: Session, owner: OwnerRead) -> list[AgentProfileRead]:
    """List the fixed roster with current registry, model alias, and unavailable-capability metadata."""
    config = await settings_public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    return list(await list_profiles(session, owner.owner_id, request.app.state.tool_registry, config))


@router.get("/api/v1/agents/profiles/{profile_id}", response_model=AgentProfileRead)
async def read_profile(
    profile_id: str, request: Request, session: Session, owner: OwnerRead,
) -> AgentProfileRead:
    """Read one owner profile and derive current capability availability from server registrations."""
    config = await settings_public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    return await get_profile(session, owner.owner_id, profile_id, request.app.state.tool_registry, config)


@router.patch("/api/v1/agents/profiles/{profile_id}", response_model=AgentProfileRead)
async def patch_profile(
    profile_id: str, payload: AgentProfilePatch, request: Request,
    session: Session, owner: OwnerWrite,
) -> AgentProfileRead:
    """Save one optimistic immutable profile revision inside the authenticated request transaction."""
    config = await settings_public.get_ai_execution_config(session, request.app.state.settings, request.app.state.redis)
    result = await update_profile_in_uow(
        session, owner.owner_id, profile_id, payload, request.app.state.tool_registry, config,
    )
    await session.commit()
    return result


@router.get("/api/v1/agent-runs", response_model=AgentRunPage)
async def read_runs(
    request: Request, session: Session, owner: OwnerRead, profile_id: str | None = None,
    conversation_id: UUID | None = None, limit: int = 25, cursor: str | None = None,
) -> AgentRunPage:
    """Return bounded owner-visible history whose linked conversations remain live for the current owner."""
    return await public.list_runs(
        session, owner.owner_id, profile_id=profile_id,
        conversation_id=conversation_id, auth_session_hash=owner.token_hash,
        session_factory=request.app.state.session_factory,
        limit=limit, cursor=cursor,
    )


@router.get("/api/v1/agent-runs/{run_id}", response_model=AgentRunRead)
async def read_run(run_id: UUID, request: Request, session: Session, owner: OwnerRead) -> AgentRunRead:
    """Read bounded run details after Chat validates linked conversation retention and session scope."""
    return await public.get_run_for_owner(
        session, run_id, owner.owner_id, owner.token_hash, request.app.state.session_factory,
    )


@router.post("/api/v1/agent-runs/{run_id}/cancel", response_model=AgentRunRead)
async def cancel_run(run_id: UUID, request: Request, session: Session, owner: OwnerWrite) -> AgentRunRead:
    """Commit cancellation only when the original owner session and live Chat parent authorize it."""
    return await public.request_cancel_for_owner(
        session, run_id, owner.owner_id, owner.token_hash, request.app.state.session_factory,
    )


@router.get("/api/v1/chat/conversations/{conversation_id}/agent-approvals", response_model=list[ApprovalRead])
async def conversation_approvals(
    conversation_id: UUID, request: Request, session: Session, owner: OwnerRead,
) -> list[ApprovalRead]:
    """Discover bounded agent actions only through a live owner-authorized Chat activity link."""
    return await public.list_conversation_approvals(
        session, request.app.state.session_factory, conversation_id,
        owner.owner_id, owner.token_hash,
    )


@router.get("/api/v1/approvals/{approval_id}", response_model=ApprovalRead)
async def read_approval(
    approval_id: UUID, request: Request, session: Session, owner: OwnerRead,
) -> ApprovalRead:
    """Read exact action detail only while the original live Chat link remains owner-authorized."""
    return await public.get_approval(
        session, request.app.state.session_factory, approval_id,
        owner.owner_id, owner.token_hash,
    )


async def _resolve_approval_request(
    approval_id: UUID, request: Request, session: AsyncSession, owner: AuthSession,
    payload: ApprovalDecisionRequest, *, approve: bool,
) -> ApprovalDecisionRead:
    """Apply one CSRF-protected immutable owner choice, then dispatch its durable generation."""
    from modules.agents.models import AgentApproval
    from sqlalchemy import select

    candidate = await session.scalar(select(AgentApproval).where(
        AgentApproval.id == approval_id, AgentApproval.owner_id == owner.owner_id,
    ))
    if candidate is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if payload.expected_argument_hash and payload.expected_argument_hash != candidate.argument_hash:
        raise HTTPException(status_code=409, detail="Approval action changed; reload before deciding")
    settings = request.app.state.settings
    try:
        profile = load_webhook_profiles(settings).get(candidate.destination_id)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Configured webhook profile is unavailable") from exc
    definition = request.app.state.tool_registry.get_tool(candidate.tool_name) if approve else None
    row, run = await resolve_decision(
        session, session_factory=request.app.state.session_factory, approval_id=approval_id,
        owner_id=owner.owner_id, auth_session_hash=owner.token_hash, approve=approve,
        expiry_hours=settings.approval_expiry_hours,
        destination_revision=(
            INTERNAL_DESTINATION[1] if candidate.tool_name in INTERNAL_WRITE_TOOLS
            else profile.revision if profile else ""
        ), definition=definition,
    )
    if run.status == "queued":
        await request.app.state.redis.enqueue_job(
            "process_agent_run", str(run.id), run.dispatch_generation,
            _job_id=f"agent-run:{run.id}:{run.dispatch_generation}",
        )
    return ApprovalDecisionRead(id=row.id, status=row.status, run_status=run.status)


@router.post("/api/v1/approvals/{approval_id}/approve", response_model=ApprovalDecisionRead)
async def approve_action(
    approval_id: UUID, payload: ApprovalDecisionRequest, request: Request,
    session: Session, owner: OwnerWrite,
) -> ApprovalDecisionRead:
    """Approve the exact displayed digest with current owner session, source and profile fences."""
    return await _resolve_approval_request(
        approval_id, request, session, owner, payload, approve=True,
    )


@router.post("/api/v1/approvals/{approval_id}/deny", response_model=ApprovalDecisionRead)
async def deny_action(
    approval_id: UUID, payload: ApprovalDecisionRequest, request: Request,
    session: Session, owner: OwnerWrite,
) -> ApprovalDecisionRead:
    """Deny the exact displayed digest and resume with a bounded denial result, never a write."""
    return await _resolve_approval_request(
        approval_id, request, session, owner, payload, approve=False,
    )
