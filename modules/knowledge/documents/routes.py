from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.database import get_session
from core.storage import storage_path
from modules.knowledge.documents import public
from modules.knowledge.documents.models import Document
from modules.knowledge.documents.schemas import (
    ContentUpdate,
    DocumentCreate,
    DocumentList,
    DocumentPatch,
    DocumentRead,
    ProviderDocumentSnapshotList,
    ProviderDocumentSnapshotRead,
    ProviderSnapshotRequest,
    VersionList,
    VersionRead,
)

router = APIRouter(
    prefix="/api/v1/documents",
    tags=["documents"],
)
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


@router.post("/provider-snapshots", response_model=list[ProviderDocumentSnapshotRead])
async def read_provider_snapshots(
    payload: ProviderSnapshotRequest, session: Session, _owner: OwnerRead
) -> list[ProviderDocumentSnapshotRead]:
    """Return exact immutable provider versions after owner-route authentication."""
    try:
        return await public.read_provider_snapshots(session, payload.version_ids)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="One or more provider versions are unavailable") from exc


@router.get("/provider-snapshots", response_model=ProviderDocumentSnapshotList)
async def list_provider_snapshots(
    session: Session,
    _owner: OwnerRead,
    source_ids: Annotated[list[UUID], Query(min_length=1, max_length=100)],
    channel_ids: Annotated[list[str] | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> ProviderDocumentSnapshotList:
    """List current provider versions with owner authorization and keyset bounds."""
    try:
        return await public.list_provider_snapshots(
            session, source_ids=source_ids, channel_ids=channel_ids,
            limit=limit, cursor=cursor,
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
    _owner: OwnerRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    source_id: UUID | None = None,
) -> DocumentList:
    """Return a bounded owner-only document page and continuation cursor."""
    items, next_cursor = await public.list_documents(session, limit, cursor, source_id)
    return DocumentList(items=[as_document_read(item) for item in items], next_cursor=next_cursor)


@router.post("", response_model=DocumentRead, status_code=201)
async def create_document(
    payload: DocumentCreate, session: Session, _owner: OwnerWrite
) -> DocumentRead:
    """Create a source-backed document under owner write authorization."""
    try:
        document = await public.create_document(session, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Document identifier already exists") from exc
    return as_document_read(document)


@router.get("/{document_id}/raw")
async def get_raw_document(
    document_id: UUID, request: Request, session: Session, _owner: OwnerRead
) -> FileResponse:
    """Stream an owner-authorized raw file with private/no-store and nosniff headers."""
    document = await public.get_document(session, document_id)
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
    document_id: UUID, session: Session, _owner: OwnerRead
) -> DocumentRead:
    """Return one owner-only document or 404 when absent."""
    document = await public.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return as_document_read(document)


@router.patch("/{document_id}", response_model=DocumentRead)
async def update_document(
    document_id: UUID,
    payload: DocumentPatch,
    session: Session,
    _owner: OwnerWrite,
) -> DocumentRead:
    """Apply non-null owner metadata fields through the document public contract."""
    document = await public.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if not payload.model_fields_set or any(
        getattr(payload, key) is None for key in payload.model_fields_set
    ):
        raise HTTPException(status_code=422, detail="At least one non-null field is required")
    document = await public.update_document(session, document, payload)
    return as_document_read(document)


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: UUID, session: Session, _owner: OwnerWrite
) -> None:
    """Delete one owner-authorized document and its supported graph data."""
    if not await public.delete_document(session, document_id):
        raise HTTPException(status_code=404, detail="Document not found")


@router.put("/{document_id}/content", response_model=DocumentRead)
async def update_content(
    document_id: UUID,
    payload: ContentUpdate,
    session: Session,
    _owner: OwnerWrite,
) -> DocumentRead:
    """Append an immutable content revision using the caller's expected version."""
    try:
        document = await public.append_content(
            session, document_id, payload.expected_version, payload.content
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
    _owner: OwnerRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> VersionList:
    """Return ascending immutable versions in a bounded owner-only page."""
    versions, next_cursor = await public.list_versions(session, document_id, limit, cursor)
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
    _owner: OwnerRead,
) -> VersionRead:
    """Return one owner-only immutable revision constrained to a valid version number."""
    version = await public.get_version(session, document_id, number)
    if version is None:
        raise HTTPException(status_code=404, detail="Document version not found")
    return VersionRead.model_validate(version, from_attributes=True)
