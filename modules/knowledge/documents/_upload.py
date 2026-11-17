"""Uploaded-document preparation and registration under original-input fences."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    and_,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._hashing import content_hash
from modules.knowledge.documents._identity import _normalized_source_proof, lock_raw_uri_identity
from modules.knowledge.documents.models import (
    Document,
    DocumentCleanupOperation,
    DocumentVersion,
    NormalizedDocumentIdentity,
)
from modules.sources.public import SourceFence


async def _upload_document_state(
    session: AsyncSession,
    *,
    source_id: UUID,
    document_id: UUID,
    external_id: str,
    raw_uri: str,
    scope: Scope,
) -> tuple[tuple[UUID, ...], tuple[tuple[UUID, datetime | None], ...]]:
    """Read bounded UUID/key collision and identity state without acquiring earlier locks.

    Caller already proved and holds original Source/access. At most two canonical rows
    and one source-key identity can exist; foreign UUID collisions disclose no metadata
    outside this owner and are rejected. Source serialization protects absent key rows.
    """
    if (
        not isinstance(source_id, UUID)
        or not isinstance(document_id, UUID)
        or type(external_id) is not str
        or not 1 <= len(external_id) <= 512
        or type(raw_uri) is not str
        or not raw_uri
    ):
        raise ValueError("Upload requires an exact Source, document, external key and raw URI")
    documents = tuple(
        (
            await session.scalars(
                select(Document.id)
                .where(
                    or_(
                        Document.id == document_id,
                        and_(Document.source_id == source_id, Document.external_id == external_id),
                    )
                )
                .order_by(Document.id)
                .limit(3)
            )
        ).all()
    )
    identity_rows = (
        await session.execute(
            select(
                NormalizedDocumentIdentity.id,
                NormalizedDocumentIdentity.workspace_id,
                NormalizedDocumentIdentity.tombstoned_at,
            )
            .where(
                NormalizedDocumentIdentity.source_id == source_id,
                NormalizedDocumentIdentity.external_id == external_id,
            )
            .order_by(NormalizedDocumentIdentity.id)
            .limit(2)
        )
    ).all()
    if len(documents) > 2 or len(identity_rows) > 1:
        raise RuntimeError("upload_document_identity_cardinality_changed")
    if any(row.workspace_id != scope.workspace_id for row in identity_rows):
        raise RuntimeError("upload_document_identity_namespace_changed")
    identities = tuple((row.id, row.tombstoned_at) for row in identity_rows)
    return documents, identities


async def _assert_upload_absence(
    session: AsyncSession,
    documents: tuple[UUID, ...],
    identities: tuple[tuple[UUID, datetime | None], ...],
    raw_uri: str,
) -> None:
    """Reject canonical/normalized reuse and globally captured raw identities without locks.

    Early preparation or late held insertion owns Source and exact URI lifecycle locks.
    The global receipt query is a conservative bool only: foreign IDs/content never leave
    Documents. Captured raw bytes cannot be republished, even after physical unlink.
    """
    if any(tombstoned_at is not None for _identity_id, tombstoned_at in identities):
        raise ValueError("Document identifier was previously deleted")
    if documents or identities:
        raise ValueError("Document identifier already exists")
    if await session.scalar(
        select(literal(True))
        .select_from(DocumentCleanupOperation)
        .where(
            DocumentCleanupOperation.raw_uri == raw_uri,
        )
        .limit(1)
    ):
        raise ValueError("This raw file identity was already deleted")


async def prepare_uploaded_document_in_uow(
    session: AsyncSession,
    *,
    source_id: UUID,
    document_id: UUID,
    external_id: str,
    raw_uri: str,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Prepare a new upload after original Source/access, before any Ingestion root writes.

    Compare full original fences nonlockingly; lock discovered Documents in UUID order,
    then exact raw URI, then existing normalized identity. Reject any UUID/key collision,
    tombstone or prior raw cleanup capture before effects. Source/URI-held absence protects
    the later initializer; no token, registry, placeholder, mutation, event or commit exists.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("upload_original_fence_required")
    await _normalized_source_proof(
        session,
        source_id,
        source_fence.generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    documents, identities = await _upload_document_state(
        session,
        source_id=source_id,
        document_id=document_id,
        external_id=external_id,
        raw_uri=raw_uri,
        scope=scope,
    )
    before = (documents, identities)
    if documents:
        await session.scalars(
            select(Document.id)
            .where(Document.id.in_(documents))
            .order_by(Document.id)
            .with_for_update()
        )
    await lock_raw_uri_identity(session, raw_uri)
    if identities:
        await session.scalars(
            select(NormalizedDocumentIdentity.id)
            .where(
                NormalizedDocumentIdentity.id.in_(
                    [identity_id for identity_id, _tombstoned_at in identities]
                ),
            )
            .order_by(NormalizedDocumentIdentity.id)
            .with_for_update()
        )
    documents, identities = await _upload_document_state(
        session,
        source_id=source_id,
        document_id=document_id,
        external_id=external_id,
        raw_uri=raw_uri,
        scope=scope,
    )
    if before != (documents, identities):
        raise RuntimeError("upload_document_preparation_changed")
    await _assert_upload_absence(session, documents, identities, raw_uri)


async def add_uploaded_document(
    session: AsyncSession,
    source_id: UUID,
    title: str,
    mime_type: str,
    raw_uri: str,
    metadata: dict[str, object],
    external_id: str,
    document_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> UUID:
    """Insert one held new upload root and empty queued version under exact original proof.

    Caller prepared exact Source/UUID/key/URI before Ingestion roots and retains those
    locks. Fresh nonlocking proof and no-republication checks do not acquire earlier
    Source/Document/URI/identity locks after I/outbox. Root inherits actual workspace;
    return UUID without commit/event/I/O. Caller owns atomic publication and raw rollback.
    """
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("upload_original_fence_required")
    await _normalized_source_proof(
        session,
        source_id,
        source_fence.generation,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    documents, identities = await _upload_document_state(
        session,
        source_id=source_id,
        document_id=document_id,
        external_id=external_id,
        raw_uri=raw_uri,
        scope=scope,
    )
    await _assert_upload_absence(session, documents, identities, raw_uri)
    document = Document(
        id=document_id,
        workspace_id=scope.workspace_id,
        source_id=source_id,
        external_id=external_id,
        title=title,
        content_type="file",
        mime_type=mime_type,
        raw_uri=raw_uri,
        metadata_json=metadata,
        current_version=1,
        content_hash=content_hash(""),
        extraction_status="queued",
    )
    session.add(document)
    await session.flush()
    session.add(
        DocumentVersion(
            document_id=document.id, version_number=1, content="", content_hash=content_hash("")
        )
    )
    await session.flush()
    return document.id
