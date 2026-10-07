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

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.storage import save_upload, storage_path
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
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.post("/sources/{source_id}/collector-credential", response_model=CollectorCredentialRead,
             dependencies=[Depends(module_dependency("ingestion"))])
async def issue_collector_credential(
    source_id: UUID,
    session: Session,
    _owner: OwnerWrite,
) -> CollectorCredentialRead:
    """Issue an owner-authorized source token only when managed activation permits it."""
    if not await connectors.allow_external_collector_credential_issue(session, source_id):
        raise HTTPException(status_code=409, detail="Managed collector grants rotate during connector activation")
    try:
        token = await public.create_collector_credential(session, source_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Source not found") from exc
    await session.commit()
    return CollectorCredentialRead(source_id=source_id, token=token)


@router.post("/batches", response_model=Receipt, status_code=202)
async def receive_batch(
    payload: ReceiveBatch,
    session: Session,
    authorization: Annotated[str | None, Header()] = None,
) -> Receipt:
    """Authenticate a collector bearer token and submit a bounded ingestion batch."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not await public.collector_can_ingest(
        session, payload.source_id, token
    ):
        raise HTTPException(status_code=401, detail="Collector authentication required")
    if not await module_is_enabled(session, "ingestion"):
        raise HTTPException(status_code=404, detail="Ingestion unavailable")
    batch, run = await public.receive_batch(session, payload, token)
    return Receipt(batch_id=batch.id, run_id=run.id, status=run.status)


@router.get("/runs/{run_id}", response_model=RunRead, dependencies=[Depends(module_dependency("ingestion"))])
async def get_run(run_id: UUID, session: Session, _owner: OwnerRead) -> RunRead:
    """Return owner-only run and stage state or 404 when the run is absent."""
    result = await public.get_run(session, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found")
    run, stages = result
    return RunRead(
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
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
) -> SourceIngestionRead:
    """Return bounded owner-only run history and current run for one source."""
    result = await public.list_source_runs(session, source_id, limit=limit, cursor=cursor)
    if result is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return result


@router.post("/runs/{run_id}/retry", response_model=Receipt, status_code=202,
             dependencies=[Depends(module_dependency("ingestion"))])
async def retry_run(run_id: UUID, payload: RetryRunRequest, session: Session, _owner: OwnerWrite) -> Receipt:
    """Retry one owner-selected stage and return its durable run identity."""
    run = await public.retry_run(session, run_id, payload.stage_key)
    if run is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found")
    return Receipt(batch_id=run.batch_id, run_id=run.id, status=run.status)


async def _intake_upload(
    request: Request, session: AsyncSession, source_id: UUID, upload: UploadFile,
) -> tuple[IngestionRun, UUID]:
    """Validate, store and enqueue one bounded upload through the shared ingestion pipeline.

    Returns the ingestion run and the owning Document ID (the existing one when the same bytes
    were already uploaded to this source). Raw bytes are removed if intake fails or deduplicates.
    """
    settings = request.app.state.settings
    if upload.size is not None and upload.size > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
    try:
        suffix, mime_type, original_name = validate_upload(
            upload.filename, upload.content_type, upload.file
        )
        # The raw blob is published before the owner row is flushed, so take a fresh
        # global admission lock immediately before crossing the filesystem boundary.
        from modules.settings.public import admit_write

        await admit_write(session, "raw_file_publication", str(source_id))
        document_id = uuid4()
        raw_uri, size, digest = await save_upload(
            settings.data_dir, upload, document_id, suffix, settings.upload_max_bytes
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
        run, created = await public.receive_file(
            session, source_id, document_id, filename, mime_type, raw_uri, size, digest
        )
    except (HTTPException, ValueError, LookupError):
        storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
        raise
    if not created:
        storage_path(settings.data_dir, raw_uri).unlink(missing_ok=True)
        existing = await documents.find_document_identity(session, source_id, f"file:{digest}")
        if existing is None:
            raise HTTPException(status_code=409, detail="This file was previously ingested and its document was deleted")
        document_id = existing
    return run, document_id


@documents_router.post("/upload", response_model=Receipt, status_code=202)
async def upload_document(
    request: Request,
    source_id: Annotated[UUID, Form()],
    upload: Annotated[UploadFile, File(alias="file")],
    session: Session,
    _owner: OwnerWrite,
) -> Receipt:
    """Validate and durably store a bounded upload, removing raw bytes if intake fails or deduplicates."""
    run, _document_id = await _intake_upload(request, session, source_id, upload)
    return Receipt(batch_id=run.batch_id, run_id=run.id, status=run.status)


async def _chat_attachment_read(
    session: AsyncSession, document_id: UUID, run_id: UUID | None = None,
) -> ChatAttachmentRead:
    """Project one Chat attachments Document into its chat-context readiness, or 404."""
    document = await documents.get_document(session, document_id)
    if document is None or not await sources.is_chat_attachments_source(session, document.source_id):
        raise HTTPException(status_code=404, detail="Chat attachment not found")
    source = await sources.get_source(session, document.source_id)
    local_only = True if source is None else source.local_only
    status: Literal["pending", "ready", "too_large", "failed"] = "failed"
    version_id: UUID | None = None
    if document.extraction_status in {"queued", "processing"}:
        status = "pending"
    elif document.extraction_status in {"ready", "succeeded"}:
        # The same current-version projection the chat selection resolver re-checks at send.
        projection = await documents.get_news_document_projection(session, document.id)
        if projection is not None:
            status = "too_large" if projection.chunks_truncated else "ready"
            version_id = projection.document_version_id
            local_only = projection.local_only
    return ChatAttachmentRead(
        document_id=document.id, source_id=document.source_id, title=document.title,
        status=status, document_version_id=version_id, local_only=local_only, run_id=run_id,
    )


@documents_router.post("/chat-attachments", response_model=ChatAttachmentRead, status_code=202)
async def upload_chat_attachment(
    request: Request,
    upload: Annotated[UploadFile, File(alias="file")],
    session: Session,
    _owner: OwnerWrite,
    share_with_model: Annotated[bool, Form()] = False,
) -> ChatAttachmentRead:
    """Store a chat attachment as an ordinary Document in one of two server-owned sources.

    ``share_with_model`` (default false) only picks between the private, local-only "Chat
    attachments" source and "Chat attachments (shared)"; the client can never name a source.
    Both are plain manual sources, so purge, export, backup and deletion treat the file exactly
    like any other uploaded Document.
    """
    source = await sources.get_or_create_chat_attachments_source(session, shared=share_with_model)
    run, document_id = await _intake_upload(request, session, source.id, upload)
    return await _chat_attachment_read(session, document_id, run.id)


@documents_router.get("/chat-attachments/{document_id}", response_model=ChatAttachmentRead)
async def get_chat_attachment(document_id: UUID, session: Session, _owner: OwnerRead) -> ChatAttachmentRead:
    """Return a chat attachment's ingest status and current version for the composer to poll."""
    return await _chat_attachment_read(session, document_id)
