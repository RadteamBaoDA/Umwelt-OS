from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.storage import save_upload, storage_path
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.public import lock_access_fence
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import public as connectors
from modules.ingestion import public
from modules.ingestion.files import validate_upload
from modules.ingestion.models import IngestionRun
from modules.ingestion.schemas import (
    ChatAttachmentRead,
    CollectorCredentialRead,
    Receipt,
    ReceiveBatch,
    RetryRunRequest,
    RunRead,
    SourceIngestionRead,
)
from modules.knowledge.documents import public as documents
from modules.settings.public import module_dependency, module_is_enabled
from modules.sources import public as sources

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])
documents_router = APIRouter(prefix="/api/v1/documents", tags=["documents"],
                             dependencies=[Depends(module_dependency("ingestion"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]


def _owner_scope(scope: WorkspaceContext) -> WorkspaceContext:
    """Reject members before Ingestion metadata/content; prepared identity is not a grant."""
    if scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    return scope


@router.post("/sources/{source_id}/collector-credential", response_model=CollectorCredentialRead,
             dependencies=[Depends(module_dependency("ingestion"))])
async def issue_collector_credential(
    source_id: UUID,
    session: Session,
    _owner: OwnerWrite,
    request: Request,
) -> CollectorCredentialRead:
    """Issue one scoped Source token under real owner/CSRF/backup/module admission.

    Members are denied before connector metadata. Literal capability is unchanged;
    managed activation denies409 and missing Source404. This HTTP caller owns commit.
    """
    scope = _owner_scope(_owner)
    enabled = request.app.state.settings.multi_workspace_enabled
    if not await connectors.allow_external_collector_credential_issue(
        session, source_id, scope=scope, multi_workspace_enabled=enabled,
    ):
        raise HTTPException(status_code=409, detail="Managed collector grants rotate during connector activation")
    try:
        token = await public.create_collector_credential(
            session, source_id, scope=scope, multi_workspace_enabled=enabled,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    await session.commit()
    return CollectorCredentialRead(workspace_id=scope.workspace_id, source_id=source_id, token=token)


@router.post("/batches", response_model=Receipt, status_code=202)
async def receive_batch(
    payload: ReceiveBatch,
    session: Session,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Receipt:
    """Authenticate exact bearer capability/Source, derive durable subject and accept a batch.

    Body contains no user/workspace authority. Preserve literal ingestion:write, token
    revocation and Source/lease rechecks in the owner intake; its wrapper owns commit.
    Invalid token401, disabled module404; exact managed-request binding remains C2.
    """
    scheme, _, token = (authorization or "").partition(" ")
    enabled = request.app.state.settings.multi_workspace_enabled
    scope = None
    if scheme.lower() == "bearer" and token:
        scope = await public.resolve_collector_job_scope(
            session, token, source_id=payload.source_id, credential_scope="ingestion:write",
            multi_workspace_enabled=enabled,
        )
    if scope is None or not await public.collector_can_ingest(
        session, payload.source_id, token, credential_scope="ingestion:write",
        scope=scope, multi_workspace_enabled=enabled,
    ):
        raise HTTPException(status_code=401, detail="Collector authentication required")
    if not await module_is_enabled(session, "ingestion", scope=scope, multi_workspace_enabled=enabled):
        raise HTTPException(status_code=404, detail="Ingestion unavailable")
    batch, run = await public.receive_batch(session, payload, token, scope=scope, multi_workspace_enabled=enabled)
    return Receipt(workspace_id=scope.workspace_id, batch_id=batch.id, run_id=run.id, status=run.status)


@router.get("/runs/{run_id}", response_model=RunRead, dependencies=[Depends(module_dependency("ingestion"))])
async def get_run(run_id: UUID, session: Session, _owner: OwnerRead, request: Request) -> RunRead:
    """Return a workspace owner's retained run/stages after actual configured admission.

    Deny members before metadata and return404 for absent/foreign roots. The public read
    intersects original actor/membership and produces no cross-workspace counts.
    """
    result = await public.get_run(session, run_id, scope=_owner_scope(_owner),
                                  multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if result is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found")
    run, stages = result
    return RunRead(
        workspace_id=run.workspace_id,
        run_id=run.id,
        source_id=run.source_id,
        status=run.status,
        stages=stages,
        error_code=run.error_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/sources/{source_id}/runs", response_model=SourceIngestionRead,
            dependencies=[Depends(module_dependency("ingestion"))])
async def list_source_runs(
    source_id: UUID,
    session: Session,
    _owner: OwnerRead,
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
) -> SourceIngestionRead:
    """Return bounded scoped owner history with principal/config-bound pagination.

    Members fail before content; Source absence/foreign identity404. Owner query applies
    lineage and cursor admission before counts/order/limit and commits nothing here.
    """
    result = await public.list_source_runs(session, source_id, limit=limit, cursor=cursor,
        scope=_owner_scope(_owner), multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if result is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return result


@router.post("/runs/{run_id}/retry", response_model=Receipt, status_code=202,
             dependencies=[Depends(module_dependency("ingestion"))])
async def retry_run(run_id: UUID, payload: RetryRunRequest, session: Session, _owner: OwnerWrite, request: Request) -> Receipt:
    """Retry the selected failed stage under workspace owner/CSRF/backup/module admission.

    The scoped owner wrapper proves original run/Source lineage and owns replay commit;
    absent/foreign runs404 and no new actor/membership replaces retained work identity.
    """
    run = await public.retry_run(session, run_id, payload.stage_key, scope=_owner_scope(_owner),
                                  multi_workspace_enabled=request.app.state.settings.multi_workspace_enabled)
    if run is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found")
    return Receipt(workspace_id=run.workspace_id, batch_id=run.batch_id, run_id=run.id, status=run.status)


@documents_router.post("/upload", response_model=Receipt, status_code=202)
async def upload_document(
    request: Request,
    source_id: Annotated[UUID, Form()],
    upload: Annotated[UploadFile, File(alias="file")],
    session: Session,
    _owner: OwnerWrite,
) -> Receipt:
    """Authorize exact Source/session before scoped raw I/O, then publish under original fences.

    Release SQL for bounded temporary/hash/atomic filesystem work. Recheck the captured
    access/Source/session without epoch upgrades before owner intake. Known rejected or
    duplicate staging bytes are deleted; uncertain commit outcomes retain bytes for the
    reference-proved orphan sweep. Filename sanitization supplies display metadata only.
    """
    scope = _owner_scope(_owner)
    settings = request.app.state.settings
    enabled = settings.multi_workspace_enabled
    session_ref = authenticated_session_ref(request)
    await lock_access_fence(session, scope=scope, multi_workspace_enabled=enabled, auth_sessions=(session_ref,))
    locked = await sources.lock_source_set(session, (source_id,), scope=scope, multi_workspace_enabled=enabled)
    source_fence = locked.fences[0]
    access_fence = locked.access_fence
    if source_fence is None or source_fence.status != "active":
        raise HTTPException(status_code=404, detail="Source not found")
    # Retain the exact original admission across filesystem work; no SQL locks over I/O.
    await session.rollback()
    if upload.size is not None and upload.size > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
    try:
        suffix, mime_type, original_name = validate_upload(
            upload.filename, upload.content_type, upload.file
        )
        from modules.settings.public import admit_write

        await admit_write(session, "raw_file_publication", str(source_id))
        await session.rollback()
        document_id = uuid4()
        raw_uri, size, digest = await save_upload(
            settings.data_dir, upload, document_id, suffix, settings.upload_max_bytes,
            workspace_id=scope.workspace_id,
        )
    except ValueError as exc:
        status = 413 if "size limit" in str(exc) else 415
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    filename = "".join(
        character
        for character in PurePosixPath(PureWindowsPath(original_name).name).name
        if ord(character) >= 32 and ord(character) != 127
    )[:255] or "upload"
    try:
        await lock_access_fence(session, scope=scope, expected=access_fence, multi_workspace_enabled=enabled,
                                auth_sessions=(session_ref,))
        current = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=enabled)
        if current != source_fence:
            raise HTTPException(status_code=409, detail="Source changed during upload")
        run, created = await public.receive_file(
            session, source_id, document_id, filename, mime_type, raw_uri, size, digest,
            scope=scope, multi_workspace_enabled=enabled,
            expected_access_fence=access_fence, expected_source_fence=source_fence,
        )
    except (HTTPException, ValueError, LookupError):
        await session.rollback()
        storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
        raise
    if not created:
        storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
    return Receipt(workspace_id=run.workspace_id, batch_id=run.batch_id, run_id=run.id, status=run.status)
