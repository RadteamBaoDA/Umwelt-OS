from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.storage import storage_path
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence
from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.documents import public
from modules.knowledge.documents.models import Document
from modules.knowledge.documents.schemas import (
    CitationTargetRead,
    ContentUpdate,
    DocumentCleanupPreparationLimitError,
    DocumentCreate,
    DocumentDeletionRead,
    DocumentList,
    DocumentPatch,
    DocumentRead,
    GadgetDocumentInteractionPatch,
    GadgetDocumentInteractionRead,
    GadgetDocumentProjectionList,
    ProviderDocumentSnapshotList,
    ProviderDocumentSnapshotRead,
    ProviderSnapshotRequest,
    VersionList,
    VersionRead,
)
from modules.settings.public import module_dependency

router = APIRouter(
    prefix="/api/v1/documents",
    tags=["documents"],
    dependencies=[Depends(module_dependency("knowledge.documents"))],
)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]
WorkspaceRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
WorkspaceWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _require_document_route_owner(scope: WorkspaceContext) -> None:
    """Deny members before owner Document IDs/content; W3 grants remain separately owned."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")


async def _lock_document_write_request(request: Request, session: AsyncSession, scope: WorkspaceContext) -> None:
    """Admit the exact request session before public Source/Document locks through commit.

    Workspace-write dependency retains CSRF/backup/module admission. Auth owns session
    persistence and share locks against logout/disable; no bearer or foreign ORM is read
    here. No external I/O/commit occurs; W4 separately fences the final response send.
    """
    _require_document_route_owner(scope)
    await lock_access_fence(session, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        auth_sessions=(authenticated_session_ref(request),))


@router.post("/provider-snapshots", response_model=list[ProviderDocumentSnapshotRead])
async def read_provider_snapshots(
    payload: ProviderSnapshotRequest, session: Session, request: Request, _owner: OwnerRead,
    workspace: WorkspaceRead,
) -> list[ProviderDocumentSnapshotRead]:
    """Return exact immutable provider versions after owner-route authentication."""
    _require_document_route_owner(workspace)
    try:
        return await public.read_provider_snapshots(session, payload.version_ids, scope=workspace,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="One or more provider versions are unavailable") from exc


@router.get("/provider-snapshots", response_model=ProviderDocumentSnapshotList)
async def list_provider_snapshots(
    session: Session,
    request: Request,
    _owner: OwnerRead,
    workspace: WorkspaceRead,
    source_ids: Annotated[list[UUID], Query(min_length=1, max_length=100)],
    channel_ids: Annotated[list[str] | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> ProviderDocumentSnapshotList:
    """List current provider versions with owner authorization and keyset bounds."""
    _require_document_route_owner(workspace)
    try:
        return await public.list_provider_snapshots(
            session, source_ids=source_ids, channel_ids=channel_ids,
            limit=limit, cursor=cursor, scope=workspace,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Provider snapshot query is invalid") from exc


def as_document_read(document: Document) -> DocumentRead:
    """Project a document ORM row into the public response schema."""
    return DocumentRead(
        id=document.id,
        source_id=document.source_id,
        external_id=document.external_id,
        title=document.title,
        content_type=document.content_type,
        mime_type=document.mime_type,
        raw_uri=document.raw_uri,
        canonical_url=document.canonical_url,
        author=document.author,
        metadata=document.metadata_json,
        current_version=document.current_version,
        content_hash=document.content_hash,
        extraction_status=document.extraction_status,
        published_at=document.published_at,
        observed_at=document.observed_at,
        language=document.language,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get("", response_model=DocumentList)
async def list_documents(
    session: Session,
    request: Request,
    scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    source_id: UUID | None = None,
) -> DocumentList:
    """Page owned retained roots after real workspace admission; members remain denied.

    Public query applies Source deletion scope before paging. Response send fencing is W4;
    selected identity never enables pending provider/gadget/deletion/citation handlers.
    """
    _require_document_route_owner(scope)
    items, next_cursor = await public.list_documents(session, limit, cursor, source_id,
        scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    return DocumentList(items=[as_document_read(item) for item in items], next_cursor=next_cursor)


@router.get("/dashboard-projections", response_model=GadgetDocumentProjectionList)
async def list_dashboard_projections(
    session: Session,
    request: Request,
    _owner: OwnerRead,
    workspace: WorkspaceRead,
    source_ids: Annotated[list[UUID], Query(min_length=1, max_length=32)],
    channel_ids: Annotated[list[str] | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    language: Annotated[str | None, Query(pattern=r"^[a-z]{2}$")] = None,
    since: datetime | None = None,
    include_dismissed: bool = False,
) -> GadgetDocumentProjectionList:
    """Return current source-scoped records through the Documents owner projection."""
    _require_document_route_owner(workspace)
    try:
        return await public.list_gadget_document_projections(
            session, source_ids=tuple(source_ids), limit=limit, cursor=cursor,
            channel_ids=tuple(channel_ids) if channel_ids is not None else None,
            language=language, since=since, include_dismissed=include_dismissed,
            scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dashboard projection scope is invalid") from exc


@router.put("/{document_id}/versions/{version_number}/interaction", response_model=GadgetDocumentInteractionRead)
async def set_dashboard_document_interaction(
    document_id: UUID,
    version_number: Annotated[int, Path(ge=1, le=2_147_483_647)],
    payload: GadgetDocumentInteractionPatch,
    session: Session,
    request: Request,
    _owner: OwnerWrite,
    workspace: WorkspaceWrite,
) -> GadgetDocumentInteractionRead:
    """Set durable owner read/bookmark state for an active exact current document version."""
    await _lock_document_write_request(request, session, workspace)
    result = await public.set_gadget_document_interaction(
        session, document_id=document_id, version_number=version_number, payload=payload,
        scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Document version is stale or unavailable")
    return result


@router.post("", response_model=DocumentRead, status_code=201)
async def create_document(
    payload: DocumentCreate, session: Session, request: Request, scope: WorkspaceWrite,
) -> DocumentRead:
    """Create an owner root/version with exact request-session locks before Source/key writes.

    Retain CSRF/backup/module admission and conflict mapping; no external I/O. Public writer
    commits scoped ready/replay atomically. W4 separately owns final response-send admission.
    """
    await _lock_document_write_request(request, session, scope)
    try:
        document = await public.create_document(session, payload, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Document identifier already exists") from exc
    return as_document_read(document)


@router.get("/{document_id}/raw")
async def get_raw_document(
    document_id: UUID, request: Request, session: Session, _owner: OwnerRead, scope: WorkspaceRead,
) -> FileResponse:
    """Keep bootstrap-only file admission while using the scoped owner root locator.

    Retain private/no-store/nosniff headers and exact storage path behavior. The root query
    denies foreign/data-purged content, but it is not W4 actual FileResponse-send proof.
    """
    _require_document_route_owner(scope)
    document = await public.get_document(session, document_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if document is None or document.raw_uri is None:
        raise HTTPException(status_code=404, detail="Raw document not found")
    try:
        path = storage_path(request.app.state.settings.data_dir, document.raw_uri)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Raw document not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Raw document not found")
    filename = str(document.metadata_json.get("filename", "download"))
    response = FileResponse(path, media_type=document.mime_type or "application/octet-stream", filename=filename)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: UUID, session: Session, request: Request, scope: WorkspaceRead,
) -> DocumentRead:
    """Read one retained owned root; foreign/deleted IDs are 404 and members are denied.

    Scope query admission preserves inactive retained metadata and Source purge privacy;
    it does not replace the separately owned exact-session final response-send fence.
    """
    _require_document_route_owner(scope)
    document = await public.get_document(session, document_id, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return as_document_read(document)


@router.patch("/{document_id}", response_model=DocumentRead)
async def update_document(
    document_id: UUID,
    payload: DocumentPatch,
    session: Session,
    request: Request,
    scope: WorkspaceWrite,
) -> DocumentRead:
    """Patch by exact ID under request-session→Source→fresh Document locks, without ORM trust.

    Require non-null metadata fields, retain scoped replay and 404 for unavailable roots.
    CSRF/backup/module admission precedes writes; W4 final response-send remains separate.
    """
    await _lock_document_write_request(request, session, scope)
    if not payload.model_fields_set or any(
        getattr(payload, key) is None for key in payload.model_fields_set
    ):
        raise HTTPException(status_code=422, detail="At least one non-null field is required")
    document = await public.update_document(session, document_id, payload, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return as_document_read(document)


@router.delete("/{document_id}", status_code=202, response_model=DocumentDeletionRead)
async def delete_document(
    document_id: UUID, session: Session, request: Request, scope: WorkspaceWrite
) -> DocumentDeletionRead:
    """Revoke document access immediately and return its durable cleanup receipt."""
    await _lock_document_write_request(request, session, scope)
    try:
        operation = await public.delete_document(
            session, document_id, scope=scope,
            multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    except DocumentCleanupPreparationLimitError as exc:
        # The session dependency rolls the whole transaction back; nothing was mutated.
        raise HTTPException(status_code=409, detail="document_cleanup_dependency_limit_exceeded") from exc
    if operation is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentDeletionRead(
        operation_id=operation.id,
        status=operation.status,
        record_status=operation.record_status,
        graph_status=operation.graph_status,
        raw_status=operation.raw_status,
        evidence_scope_status=operation.evidence_scope_status,
        copied_status=operation.copied_status,
        chat_status=operation.chat_status,
        chat_error_code=operation.chat_error_code,
        memory_status=operation.memory_status,
        memory_error_code=operation.memory_error_code,
        memory_unresolved_count=operation.memory_unresolved_count,
        memory_cache_pending=operation.memory_cache_pending,
        agent_status=operation.agent_status,
        agent_error_code=operation.agent_error_code,
        agent_unresolved_count=operation.agent_unresolved_count,
        agent_waiting_for_lease=operation.agent_waiting_for_lease,
        materialization_status=operation.materialization_status,
        materialization_error_code=operation.materialization_error_code,
        materialization_unresolved_count=operation.materialization_unresolved_count,
        brief_status=operation.brief_status,
        brief_error_code=operation.brief_error_code,
        brief_unresolved_count=operation.brief_unresolved_count,
        immediate_access_revoked=True,
        error_code=operation.error_code,
        copied_error_code=operation.copied_error_code,
    )


@router.get("/deletion-operations/{operation_id}", response_model=DocumentDeletionRead)
async def get_deletion_operation(
    operation_id: UUID, session: Session, request: Request, scope: WorkspaceRead,
) -> DocumentDeletionRead:
    """Return owner-only durable status; a receipt whose authority is missing/stale reports action-required."""
    _require_document_route_owner(scope)
    flag = request.app.state.settings.multi_workspace_enabled
    operation = await public.get_document_cleanup_operation(
        session, operation_id, scope=scope, multi_workspace_enabled=flag)
    if operation is None:
        raise HTTPException(status_code=404, detail="Document cleanup operation not found")
    authority_error = await public.cleanup_authority_error(
        session, operation, scope=scope, multi_workspace_enabled=flag)
    return DocumentDeletionRead(
        operation_id=operation.id,
        status=operation.status,
        record_status=operation.record_status,
        graph_status=operation.graph_status,
        raw_status=operation.raw_status,
        evidence_scope_status=operation.evidence_scope_status,
        copied_status=operation.copied_status,
        chat_status=operation.chat_status,
        chat_error_code=operation.chat_error_code,
        memory_status=operation.memory_status,
        memory_error_code=operation.memory_error_code,
        memory_unresolved_count=operation.memory_unresolved_count,
        memory_cache_pending=operation.memory_cache_pending,
        agent_status=operation.agent_status,
        agent_error_code=operation.agent_error_code,
        agent_unresolved_count=operation.agent_unresolved_count,
        agent_waiting_for_lease=operation.agent_waiting_for_lease,
        materialization_status=operation.materialization_status,
        materialization_error_code=operation.materialization_error_code,
        materialization_unresolved_count=operation.materialization_unresolved_count,
        brief_status=operation.brief_status,
        brief_error_code=operation.brief_error_code,
        brief_unresolved_count=operation.brief_unresolved_count,
        immediate_access_revoked=True,
        error_code=authority_error or operation.error_code,
        copied_error_code=operation.copied_error_code,
    )


@router.put("/{document_id}/content", response_model=DocumentRead)
async def update_content(
    document_id: UUID,
    payload: ContentUpdate,
    session: Session,
    request: Request,
    scope: WorkspaceWrite,
) -> DocumentRead:
    """Append under exact request-session admission before Source/Document locks and commit.

    Preserve same-content retry no-op before stale-version CAS, 409 conflict and unavailable
    404. CSRF/backup/module checks remain; W4 final response-send proof is separately owned.
    """
    await _lock_document_write_request(request, session, scope)
    try:
        document = await public.append_content(
            session, document_id, payload.expected_version, payload.content,
            scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return as_document_read(document)


@router.get("/{document_id}/versions", response_model=VersionList)
async def list_versions(
    document_id: UUID,
    session: Session,
    request: Request,
    scope: WorkspaceRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> VersionList:
    """Page exact historical owner revisions through retained root/Source privacy predicates.

    Deny members before enrichment, preserve ascending ordering/404 and defer actual-send
    admission to W4. Workspace context alone does not enable shared-document history.
    """
    _require_document_route_owner(scope)
    versions, next_cursor = await public.list_versions(session, document_id, limit, cursor,
        scope=scope, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if versions is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return VersionList(
        items=[VersionRead.model_validate(version, from_attributes=True) for version in versions],
        next_cursor=next_cursor,
    )


@router.get("/{document_id}/versions/{number}", response_model=VersionRead)
async def get_version(
    document_id: UUID,
    number: Annotated[int, Path(ge=1, le=2147483647)],
    session: Session,
    request: Request,
    scope: WorkspaceRead,
) -> VersionRead:
    """Read an exact historical owner revision without current-version substitution or grants.

    Deny members before root/version query; scoped unavailable versions are 404. Source purge
    privacy and retained inactive eligibility stay in owner query, with W4 send work separate.
    """
    _require_document_route_owner(scope)
    version = await public.get_version(session, document_id, number, scope=scope,
        multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if version is None:
        raise HTTPException(status_code=404, detail="Document version not found")
    return VersionRead.model_validate(version, from_attributes=True)


@router.get("/{document_id}/citation-target", response_model=CitationTargetRead)
async def get_citation_target(
    document_id: UUID,
    session: Session,
    request: Request,
    _owner: OwnerRead,
    workspace: WorkspaceRead,
    document_version_id: UUID,
    chunk_id: UUID,
) -> CitationTargetRead:
    """Resolve a citation only while its exact versioned chunk remains owner-readable.

    This reader uses Documents' active-source fence and accepts retained historical versions;
    it never remaps a citation to a newer current version. Owner authentication is not replaced
    by the version IDs supplied in the navigation URL.
    """
    _require_document_route_owner(workspace)
    chunks = await public.read_chat_evidence_chunks(
        session, [(document_version_id, chunk_id)], require_current_version=False,
        scope=workspace, multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled,
    )
    if not chunks or chunks[0].document_id != document_id:
        raise HTTPException(status_code=404, detail="Citation evidence is no longer available")
    chunk = chunks[0]
    return CitationTargetRead(
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        version_number=chunk.version_number,
        chunk_id=chunk.chunk_id,
        title=chunk.title,
        excerpt=chunk.content[:1000],
        observed_at=chunk.observed_at,
    )
