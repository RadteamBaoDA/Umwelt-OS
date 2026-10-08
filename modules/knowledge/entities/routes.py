from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.entities import corrections, public
from modules.knowledge.entities.corrections import CorrectionConflictError
from modules.knowledge.entities.schemas import (
    AliasCreate,
    EntityCorrectionPreview,
    EntityCorrectionResult,
    EntityCreate,
    EntityEvidencePage,
    EntityExtractionStatus,
    EntityMergeRequest,
    EntityPage,
    EntityPatch,
    EntityRead,
    EntityRelationshipReviewRequest,
    EntityRelationshipReviewResult,
    EntityReviewAssignmentRequest,
    EntityReviewAssignmentResult,
    EntityReviewPage,
    EntitySplitRequest,
    EntitySuppressionRequest,
)
from modules.knowledge.public import KnowledgeService
from modules.knowledge.relationships.schemas import NeighborPage
from modules.settings.public import module_dependency

router = APIRouter(tags=["knowledge"], dependencies=[Depends(module_dependency("knowledge.entities"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


@router.get("/api/v1/entities/extractions/{document_version_id}", response_model=EntityExtractionStatus)
async def get_extraction_status(
    document_version_id: UUID, session: Session, _owner: OwnerRead, request: Request, scope: WorkspaceRead,
) -> EntityExtractionStatus:
    """Read owner-only extraction status for one version; return 404 when no work exists."""
    result = await public.get_extraction_status(
        session, document_version_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Entity extraction status not found")
    return result


@router.get("/api/v1/entities", response_model=EntityPage)
async def list_entities(
    session: Session,
    _owner: OwnerRead,
    request: Request,
    scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    entity_type: Annotated[str | None, Query(alias="type", max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=300)] = None,
) -> EntityPage:
    """Return an owner-authenticated, bounded entity page with optional filters."""
    return await KnowledgeService(
        session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ).entities(limit=limit, cursor=cursor, entity_type=entity_type, query=q)


@router.get("/api/v1/entities/review", response_model=EntityReviewPage)
async def list_review_candidates(
    session: Session, _owner: OwnerRead, request: Request, scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> EntityReviewPage:
    """List bounded owner-review candidates and map invalid cursors to 422."""
    try:
        return await KnowledgeService(
            session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ).entity_review(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/v1/entities/review/{candidate_id}/assign", response_model=EntityReviewAssignmentResult)
async def assign_review_candidate(
    candidate_id: UUID, payload: EntityReviewAssignmentRequest,
    session: Session, owner: OwnerWrite, request: Request, scope: WorkspaceWrite,
) -> EntityReviewAssignmentResult:
    """Apply a write-authorized candidate assignment and map stale review conflicts to 409."""
    try:
        return await KnowledgeService(
            session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ).assign_review_candidate(candidate_id, payload, actor_id=owner.owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REVIEW_CONFLICT", "message": str(exc), "details": {}}) from exc


@router.post("/api/v1/entities/review/{candidate_id}/resolve-relationship", response_model=EntityRelationshipReviewResult)
async def resolve_relationship_review(
    candidate_id: UUID, payload: EntityRelationshipReviewRequest,
    session: Session, owner: OwnerWrite, request: Request, scope: WorkspaceWrite,
) -> EntityRelationshipReviewResult:
    """Resolve a write-authorized relationship candidate against its snapshot evidence."""
    try:
        return await KnowledgeService(
            session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ).resolve_relationship_review(candidate_id, payload, actor_id=owner.owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_REVIEW_CONFLICT", "message": str(exc), "details": {}}) from exc


@router.post("/api/v1/entities", response_model=EntityRead, status_code=201)
async def create_entity(
    payload: EntityCreate, session: Session, owner: OwnerWrite, request: Request, scope: WorkspaceWrite,
) -> EntityRead:
    """Create an owner-authored entity and map duplicate aliases to 409."""
    try:
        return await public.create_entity(
            session, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Entity alias already exists") from exc


@router.get("/api/v1/entities/{entity_id}/neighbors", response_model=NeighborPage)
async def get_neighbors(
    entity_id: UUID,
    session: Session,
    _owner: OwnerRead,
    request: Request,
    scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=2, le=100)] = 50,
    cursor: str | None = Query(default=None, max_length=512),
) -> NeighborPage:
    """Return bounded owner-only neighbors or 404 when the focus entity is absent."""
    try:
        result = await KnowledgeService(
            session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ).entity_neighbors(entity_id, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return result


@router.post("/api/v1/entities/{entity_id}/aliases", response_model=EntityRead, status_code=201)
async def add_alias(
    entity_id: UUID, payload: AliasCreate, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
) -> EntityRead:
    """Add an alias through the owner write contract with redirect/conflict status mapping."""
    try:
        result = await public.add_alias(
            session, entity_id, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except public.TerminalEntityConflict as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except public.RedirectedEntityConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REDIRECTED", "message": str(exc), "details": {}}) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Alias already exists for this entity") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return result


@router.delete("/api/v1/entities/{entity_id}/aliases/{alias_id}", status_code=204)
async def delete_alias(
    entity_id: UUID, alias_id: UUID, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
    reason: Annotated[str, Query(min_length=1, max_length=300)] = "owner_alias_delete",
) -> None:
    """Delete one alias using the authenticated owner ID and bounded audit reason."""
    try:
        if not await public.delete_alias(
            session, entity_id, alias_id, reason=reason, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ):
            raise HTTPException(status_code=404, detail="Alias not found")
    except public.TerminalEntityConflict as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except public.RedirectedEntityConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REDIRECTED", "message": str(exc), "details": {}}) from exc


@router.get("/api/v1/entities/{entity_id}", response_model=EntityRead)
async def get_entity(
    entity_id: UUID, session: Session, _owner: OwnerRead, request: Request, scope: WorkspaceRead,
) -> EntityRead:
    """Return an owner-only entity projection or 404 when its identity is unavailable."""
    entity = await KnowledgeService(
        session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    ).entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("/api/v1/entities/{entity_id}/evidence", response_model=EntityEvidencePage)
async def list_evidence(
    entity_id: UUID, session: Session, _owner: OwnerRead, request: Request, scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> EntityEvidencePage:
    """Return bounded owner-only evidence for an entity with invalid cursors mapped to 422."""
    try:
        page = await KnowledgeService(
            session, scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ).entity_evidence(entity_id, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return page


@router.post("/api/v1/entities/{entity_id}/corrections/merge-preview", response_model=EntityCorrectionPreview)
async def preview_merge(
    entity_id: UUID, payload: EntityMergeRequest, session: Session, _owner: OwnerRead,
    request: Request, scope: WorkspaceRead,
) -> EntityCorrectionPreview:
    """Preview merge scope and conflicts without applying a correction."""
    return await corrections.preview_merge(
        session, entity_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )


@router.post("/api/v1/entities/{entity_id}/corrections/split-preview", response_model=EntityCorrectionPreview)
async def preview_split(
    entity_id: UUID, payload: EntitySplitRequest, session: Session, _owner: OwnerRead,
    request: Request, scope: WorkspaceRead,
) -> EntityCorrectionPreview:
    """Preview split scope and conflicts without applying a correction."""
    return await corrections.preview_split(
        session, entity_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )


@router.post("/api/v1/entities/{entity_id}/merge", response_model=EntityCorrectionResult)
async def merge_entity(
    entity_id: UUID, payload: EntityMergeRequest, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
) -> EntityCorrectionResult:
    """Apply a merge as the authenticated owner and map correction conflicts to 404/409."""
    try:
        return await corrections.merge_entity(
            session, entity_id, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except CorrectionConflictError as exc:
        if exc.conflict.code == "entity_missing":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail={
            "code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc),
            "details": {"conflicts": [exc.conflict.model_dump(mode="json")]},
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc), "details": {}}) from exc


@router.post("/api/v1/entities/{entity_id}/split", response_model=EntityCorrectionResult)
async def split_entity(
    entity_id: UUID, payload: EntitySplitRequest, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
) -> EntityCorrectionResult:
    """Apply an evidence split as the authenticated owner with structured conflicts."""
    try:
        return await corrections.split_entity(
            session, entity_id, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except CorrectionConflictError as exc:
        if exc.conflict.code == "entity_missing":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail={
            "code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc),
            "details": {"conflicts": [exc.conflict.model_dump(mode="json")]},
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc), "details": {}}) from exc


@router.post("/api/v1/entities/{entity_id}/suppressions", response_model=EntityCorrectionResult)
async def suppress_candidates(
    entity_id: UUID, payload: EntitySuppressionRequest, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
) -> EntityCorrectionResult:
    """Persist owner suppression decisions for selected extraction candidates."""
    try:
        return await corrections.suppress_candidates(
            session, entity_id, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except CorrectionConflictError as exc:
        if exc.conflict.code == "entity_missing":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail={
            "code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc),
            "details": {"conflicts": [exc.conflict.model_dump(mode="json")]},
        }) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_CORRECTION_CONFLICT", "message": str(exc), "details": {}}) from exc


@router.patch("/api/v1/entities/{entity_id}", response_model=EntityRead)
async def update_entity(
    entity_id: UUID, payload: EntityPatch, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
) -> EntityRead:
    """Apply a write-authorized revision-fenced entity update or return conflict/not-found."""
    try:
        entity = await public.update_entity(
            session, entity_id, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except public.TerminalEntityConflict as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.delete("/api/v1/entities/{entity_id}", status_code=204)
async def delete_entity(
    entity_id: UUID, session: Session, owner: OwnerWrite,
    request: Request, scope: WorkspaceWrite,
    reason: Annotated[str, Query(min_length=1, max_length=300)] = "owner_entity_delete",
) -> None:
    """Delete the canonical entity with owner reason and structured closure-conflict mapping."""
    try:
        if not await public.delete_entity(
            session, entity_id, reason=reason, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        ):
            raise HTTPException(status_code=404, detail="Entity not found")
    except public.TerminalEntityConflict as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorrectionConflictError as exc:
        if exc.conflict.code == "entity_missing":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail={"code": exc.conflict.code.upper(), "message": str(exc), "details": {"conflict": exc.conflict.model_dump(mode="json")}}) from exc
    except public.RedirectedEntityConflict as exc:
        raise HTTPException(status_code=409, detail={"code": "ENTITY_REDIRECTED", "message": str(exc), "details": {}}) from exc
