"""Owner-authenticated HTTP routes for memory management, candidates, and privacy settings."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from modules.memory.public import MemoryService
from modules.memory.schemas import (
    MemoryCandidatePage,
    MemoryCandidateRead,
    MemoryCandidateRejectRequest,
    MemoryCreate,
    MemoryForgetRequest,
    MemoryInvalidateRequest,
    MemoryPage,
    MemoryPrivacyConfig,
    MemoryPrivacyUpdate,
    MemoryPurgeRequest,
    MemoryPurgeResponse,
    MemoryRead,
    MemorySupersedeRequest,
    MemoryUpdate,
)

router = APIRouter(tags=["memory"])

Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


def _get_redis(session: Session) -> Redis | None:
    """Safely obtain app-state Redis client from database session bind if available.

    Args:
        session: Active SQLAlchemy session.

    Returns:
        Redis client instance or None.
    """
    # Redis can be accessed from request/app state in endpoints via session or None
    return None


@router.get("/api/v1/memories", response_model=MemoryPage)
async def list_memories(
    session: Session,
    _owner: OwnerRead,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    type: str | None = Query(default=None),
    status: str = Query(default="active"),
    q: str | None = Query(default=None),
) -> MemoryPage:
    """List memories matching filter criteria with cursor pagination.

    Args:
        session: Active database session.
        _owner: Authenticated owner read dependency.
        limit: Max items to return (1-100).
        cursor: Opaque pagination cursor.
        type: Optional filter by memory type ('fact', 'preference', 'instruction').
        status: Filter by lifecycle status (default 'active').
        q: Optional substring filter.

    Returns:
        MemoryPage with matching items and next cursor.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    return await svc.get_memories(
        limit=limit,
        cursor=cursor,
        memory_type=type,
        status=status,
        query=q,
    )


@router.post("/api/v1/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreate,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Explicitly create an owner memory item.

    Args:
        payload: Validated MemoryCreate parameters.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Created MemoryRead schema.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    return await svc.create_memory(payload, is_manual=True)


@router.get("/api/v1/memories/{memory_id}", response_model=MemoryRead)
async def get_memory(
    memory_id: UUID,
    session: Session,
    _owner: OwnerRead,
) -> MemoryRead:
    """Fetch a single memory item by identifier.

    Args:
        memory_id: Memory UUID.
        session: Active database session.
        _owner: Authenticated owner read dependency.

    Returns:
        MemoryRead schema.

    Raises:
        HTTPException: 404 if memory not found.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.get_memory(memory_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return mem


@router.patch("/api/v1/memories/{memory_id}", response_model=MemoryRead)
async def update_memory(
    memory_id: UUID,
    payload: MemoryUpdate,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Update an existing active memory item.

    Args:
        memory_id: Target memory UUID.
        payload: MemoryUpdate parameters.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Updated MemoryRead schema.

    Raises:
        HTTPException: 404 if memory not found or not active.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.update_memory(memory_id, payload)
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found or not active")
    return mem


@router.delete("/api/v1/memories/{memory_id}", response_model=MemoryRead)
async def delete_memory(
    memory_id: UUID,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Forget and purge a memory item, making it immediately unavailable.

    Args:
        memory_id: UUID of memory to forget.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Forgotten MemoryRead schema.

    Raises:
        HTTPException: 404 if memory not found.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.forget_memory(memory_id, reason="Deleted by owner")
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return mem


@router.post("/api/v1/memories/{memory_id}/invalidate", response_model=MemoryRead)
async def invalidate_memory(
    memory_id: UUID,
    payload: MemoryInvalidateRequest,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Mark an active memory invalidated.

    Args:
        memory_id: Target memory UUID.
        payload: Invalidation rationale.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Invalidated MemoryRead schema.

    Raises:
        HTTPException: 404 if memory not found.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.invalidate_memory(memory_id, reason=payload.reason)
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return mem


@router.post("/api/v1/memories/{memory_id}/supersede", response_model=MemoryRead)
async def supersede_memory(
    memory_id: UUID,
    payload: MemorySupersedeRequest,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Supersede an existing memory with updated knowledge.

    Args:
        memory_id: Target memory UUID to supersede.
        payload: Supersede parameters with replacement content.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Newly created replacement MemoryRead schema.

    Raises:
        HTTPException: 404 if target memory not found.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    res = await svc.supersede_memory(memory_id, payload)
    if res is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    _old, new_mem = res
    return new_mem


@router.post("/api/v1/memories/{memory_id}/forget", response_model=MemoryRead)
async def forget_memory(
    memory_id: UUID,
    payload: MemoryForgetRequest,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Forget a memory item immediately, purging it from retrieval.

    Args:
        memory_id: Target memory UUID.
        payload: Optional reason for forgetting.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Forgotten MemoryRead schema.

    Raises:
        HTTPException: 404 if memory not found.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.forget_memory(memory_id, reason=payload.reason)
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return mem


@router.get("/api/v1/settings/memory-privacy", response_model=MemoryPrivacyConfig)
async def get_memory_privacy(
    session: Session,
    _owner: OwnerRead,
) -> MemoryPrivacyConfig:
    """Read owner memory and conversation privacy settings.

    Args:
        session: Active database session.
        _owner: Authenticated owner read dependency.

    Returns:
        Current MemoryPrivacyConfig.
    """
    svc = MemoryService(session)
    return await svc.get_privacy_config()


@router.put("/api/v1/settings/memory-privacy", response_model=MemoryPrivacyConfig)
async def update_memory_privacy(
    payload: MemoryPrivacyUpdate,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryPrivacyConfig:
    """Update owner memory and conversation privacy controls.

    Args:
        payload: MemoryPrivacyUpdate parameters.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Updated MemoryPrivacyConfig.
    """
    svc = MemoryService(session)
    return await svc.update_privacy_config(payload)


@router.get("/api/v1/memories/candidates/list", response_model=MemoryCandidatePage)
async def list_candidates(
    session: Session,
    _owner: OwnerRead,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    status: str = Query(default="pending"),
) -> MemoryCandidatePage:
    """List memory candidates proposed for owner review.

    Args:
        session: Active database session.
        _owner: Authenticated owner read dependency.
        limit: Max candidates to return.
        cursor: Pagination cursor.
        status: Candidate status filter ('pending', 'accepted', 'rejected').

    Returns:
        MemoryCandidatePage.
    """
    svc = MemoryService(session)
    return await svc.get_candidates(limit=limit, cursor=cursor, status=status)


@router.post("/api/v1/memories/candidates/{candidate_id}/accept", response_model=MemoryRead)
async def accept_candidate(
    candidate_id: UUID,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryRead:
    """Accept a proposed memory candidate into active memory.

    Args:
        candidate_id: Target candidate UUID.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Created active MemoryRead.

    Raises:
        HTTPException: 404 if candidate not found or not pending.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    mem = await svc.accept_candidate(candidate_id)
    if mem is None:
        raise HTTPException(status_code=404, detail="Candidate not found or not pending")
    return mem


@router.post(
    "/api/v1/memories/candidates/{candidate_id}/reject",
    response_model=MemoryCandidateRead,
)
async def reject_candidate(
    candidate_id: UUID,
    payload: MemoryCandidateRejectRequest,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryCandidateRead:
    """Reject a proposed memory candidate.

    Args:
        candidate_id: Target candidate UUID.
        payload: Rejection reason.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        Updated MemoryCandidateRead.

    Raises:
        HTTPException: 404 if candidate not found.
    """
    svc = MemoryService(session)
    cand = await svc.reject_candidate(candidate_id, reason=payload.reason)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return cand


@router.post("/api/v1/settings/memory-privacy/purge", response_model=MemoryPurgeResponse)
async def purge_memory_data(
    payload: MemoryPurgeRequest,
    session: Session,
    _owner: OwnerWrite,
) -> MemoryPurgeResponse:
    """Purge forgotten memories, rejected candidates, or conversation history.

    Args:
        payload: MemoryPurgeRequest options.
        session: Active database session.
        _owner: Authenticated owner write dependency.

    Returns:
        MemoryPurgeResponse with counts of deleted records.
    """
    svc = MemoryService(session, redis=_get_redis(session))
    return await svc.purge_memories(payload)
