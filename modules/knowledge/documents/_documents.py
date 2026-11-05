"""Document CRUD, owner listing/cursors, content append and retention purge of old versions."""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    SQLColumnExpression,
    and_,
    delete,
    desc,
    func,
    not_,
    or_,
    select,
    tuple_,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, ForbiddenError, ValidationFailedError
from core.pagination import decode_cursor, encode_cursor
from core.realtime import commit_with_replay, make_knowledge_change
from core.workspaces.schemas import (
    AccessFence,
    AdminWorkspaceReadContext,
    ResourceAccessProjection,
    Scope,
    WorkspaceContext,
)
from modules.knowledge.documents._chunks import add_content_chunks
from modules.knowledge.documents._hashing import content_hash_async
from modules.knowledge.documents._ready import _publish_document_ready
from modules.knowledge.documents._scope import (
    _admit_document_scope,
    _document_scope,
    _read_document_source_id,
    _require_document_owner,
)
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentInteraction,
    DocumentVersion,
    NormalizedDocumentIdentity,
)
from modules.knowledge.documents.schemas import (
    DocumentCreate,
    DocumentPatch,
)
from modules.sources import public as sources
from modules.sources.public import SourceReadProjection as Source


async def observability_quality_summary(
    session: AsyncSession, *, instance_operator: bool,
) -> dict[str, int]:
    """Return global document counts only to an explicitly admitted instance operator."""
    if instance_operator is not True:
        raise ForbiddenError("Instance operator required")
    document_count = int(await session.scalar(select(func.count()).select_from(Document)) or 0)
    orphan_chunks = int(await session.scalar(select(func.count()).select_from(DocumentChunk).outerjoin(
        DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id,
    ).where(DocumentVersion.id.is_(None))) or 0)
    return {"document_count": document_count, "orphan_chunks": orphan_chunks}


async def _assert_new_document_key(
    session: AsyncSession, source_id: UUID, external_id: str | None, *, scope: Scope,
) -> None:
    """Reject an occupied/tombstoned exact external key under the caller-held Source lock.

    Lock an existing canonical root before its normalized identity, retaining absence under
    Source serialization. Nullable manual keys have no shared namespace. No winner adoption,
    tombstone resurrection, placeholder, mutation, commit or foreign ORM occurs.
    """
    if external_id is None:
        return
    document = await session.scalar(select(Document).where(
        Document.source_id == source_id, Document.external_id == external_id,
    ).order_by(Document.id).with_for_update().execution_options(populate_existing=True))
    identity = await session.scalar(select(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.source_id == source_id, NormalizedDocumentIdentity.external_id == external_id,
    ).order_by(NormalizedDocumentIdentity.id).with_for_update().execution_options(populate_existing=True))
    if any(row is not None and row.workspace_id != scope.workspace_id for row in (document, identity)):
        raise RuntimeError("document_key_namespace_changed")
    if identity is not None and identity.tombstoned_at is not None:
        raise ValueError("Document identifier was previously deleted")
    if document is not None or identity is not None:
        raise ValueError("Document identifier already exists")


async def create_document(
    session: AsyncSession, payload: DocumentCreate, *, scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document:
    """Create an owner workspace root/version under early Source/key locks and scoped replay.

    Active Source and real owner admission precede all writes. Reject occupied/tombstoned
    external keys; initial chunks and two ready events commit atomically with the root/replay.
    Return own ORM for this module's route only. No external I/O or cleanup authority change.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Manual document creation requires a workspace owner")
    _require_document_owner(scope)
    locked = await sources.lock_source_set(session, (payload.source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    source_fence, access_fence = locked.fences[0], locked.access_fence
    if source_fence.status != "active":
        raise ValueError("Cannot add documents to an inactive source")
    await _assert_new_document_key(session, payload.source_id, payload.external_id, scope=scope)
    digest = await content_hash_async(payload.content)
    document = Document(
        workspace_id=scope.workspace_id,
        source_id=payload.source_id,
        external_id=payload.external_id,
        title=payload.title,
        metadata_json=payload.metadata,
        current_version=1,
        content_hash=digest,
    )
    session.add(document)
    try:
        await session.flush()
        version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content=payload.content,
                content_hash=digest,
        )
        session.add(version)
        if await add_content_chunks(session, version):
            await _publish_document_ready(session, document, version, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
        # Load generated metadata under the original locks, before the final commit releases them.
        await session.flush()
        await session.refresh(document)
        await commit_with_replay(
            session,
            [make_knowledge_change(payload.source_id, document.id, 1, scope=scope)],
            scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        )
    except IntegrityError:
        await session.rollback()
        raise
    return document


async def document_metadata(
    session: AsyncSession, document_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> dict[UUID, tuple[str, str | None]]:
    """Return ``{document_id: (title, mime_type)}`` for the given ids (metadata only, never content).

    Read-only batch projection for the automations producer sweep; callers pass at most one
    sweep page of ids. Unknown ids are simply absent.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(document_ids) > 100 or len(set(document_ids)) != len(document_ids):
        raise ValueError("Document metadata page must contain at most 100 unique IDs")
    rows = await session.execute(select(Document.id, Document.title, Document.mime_type).where(
        Document.id.in_(document_ids), *_document_scope(scope)).limit(100))
    return {row[0]: (row[1], row[2]) for row in rows.all()}


async def get_document(
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> Document | None:
    """Read one owned retained root after real admission, without granting member visibility.

    Workspace and Source-owned deletion lineage precede the ID query. Inactive retained
    metadata remains eligible except unfinished/failed data purges. Own ORM serves only
    Documents routes; external callers must migrate to detached owner projections.
    """
    await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, allow_member=True)
    return await session.scalar(select(Document).where(
        Document.id == document_id, *_document_scope(scope, allow_member=True),
    ).execution_options(populate_existing=True))


async def read_document_access_projection(
    session: AsyncSession, resource_id: UUID, *, scope: Scope,
) -> ResourceAccessProjection | None:
    """Return the share-binding revision (current_version) of one in-scope Document, else None.

    Caller admitted ``scope``; a member sees only active-grant Documents. Detached, no locks.
    """
    row = (await session.execute(select(Document.workspace_id, Document.current_version).where(
        Document.id == resource_id, *_document_scope(scope, allow_member=True),
    ))).one_or_none()
    if row is None:
        return None
    return ResourceAccessProjection(
        workspace_id=row[0], resource_id=resource_id, resource_revision=row[1], available=True)


async def existing_document_ids(
    session: AsyncSession, document_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Return sorted IDs for at most 100 retained documents in the admitted owner workspace.

    This identifier-only helper is safe for cross-domain selection: SQL applies workspace and
    unfinished-purge fences before returning any ID. It neither locks rows nor reads content.
    """
    if len(document_ids) > 100 or any(type(identifier) is not UUID for identifier in document_ids):
        raise ValueError("Document identity batch exceeds 100 IDs")
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if not document_ids:
        return []
    rows = await session.scalars(select(Document.id).where(
        Document.id.in_(set(document_ids)), *_document_scope(scope),
    ).order_by(Document.id).limit(100))
    return list(rows.all())


async def list_indexable_workspace_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Discover at most 100 workspaces with ready current chunks for automatic indexing.

    Results are identity-only candidates, not authority. The Search worker must resolve the
    durable owner and perform current workspace/module/config/privacy admission before effects.
    No content, credentials, foreign workspace/settings ORM, commit or provider call is used.
    """
    if type(limit) is not int or limit < 1 or (after is not None and type(after) is not UUID):
        raise ValueError("Workspace discovery requires a positive limit and UUID cursor")
    statement = (
        select(Document.workspace_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
            Source.status == "active", Source.local_only.is_(False),
        )
    )
    if after is not None:
        statement = statement.where(Document.workspace_id > after)
    result = await session.scalars(
        statement.distinct().order_by(Document.workspace_id).limit(min(limit, 100)),
    )
    return tuple(result.all())


async def list_ready_document_workspace_ids(
    session: AsyncSession, *, after: UUID | None = None, limit: int = 100,
) -> tuple[UUID, ...]:
    """Discover bounded workspace identities with a ready current Document version.

    This is a worker candidate seam only. A returned workspace conveys no authority; the
    caller must resolve and admit its actual owner and module scope before any protected read
    or effect. The query exposes no content, chunks, Source, Workspace or account state.
    """
    if type(limit) is not int or limit < 1 or (after is not None and type(after) is not UUID):
        raise ValueError("Workspace discovery requires a positive limit and UUID cursor")
    statement = (
        select(Document.workspace_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.current_version == DocumentVersion.version_number,
            Document.extraction_status.in_(("ready", "succeeded")),
        )
    )
    if after is not None:
        statement = statement.where(Document.workspace_id > after)
    result = await session.scalars(
        statement.distinct().order_by(Document.workspace_id).limit(min(limit, 100)),
    )
    return tuple(result.all())


def _encode_document_owner_cursor(
    position: str, *, kind: str, fence: AccessFence, resource_id: UUID | None,
) -> str:
    """Wrap an existing keyset position in actor/workspace/epoch and exact selector context.

    This public wire value is neither a grant nor a signed snapshot. Source/root predicates
    remain mandatory on every page. No secret/key infrastructure or legacy fallback exists.
    """
    payload = [kind, str(fence.workspace_id), fence.user_id, fence.membership_revision,
               fence.configuration_revision, str(resource_id) if resource_id is not None else None, position]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_document_owner_cursor(
    cursor: str, *, kind: str, fence: AccessFence, resource_id: UUID | None,
) -> str:
    """Validate canonical bounded cursor context before returning the existing keyset position.

    Legacy/unscoped, malformed and other actor/workspace/filter/document cursors are 422;
    same-context stale membership/configuration is 409. Cursor edits confer no authority:
    real admission and Source/root predicates still precede each page query.
    """
    try:
        if type(cursor) is not str or not 1 <= len(cursor) <= 2048 or "=" in cursor:
            raise ValueError("Invalid cursor encoding")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Invalid cursor encoding")
        payload = json.loads(raw)
        if (not isinstance(payload, list) or len(payload) != 7 or type(payload[2]) is not int
                or type(payload[3]) is not int or payload[3] <= 0
                or type(payload[4]) is not int or payload[4] <= 0
                or type(payload[6]) is not str or not payload[6]):
            raise ValueError("Invalid cursor shape")
        if [payload[0], payload[1], payload[2], payload[5]] != [
            kind, str(fence.workspace_id), fence.user_id, str(resource_id) if resource_id is not None else None,
        ]:
            raise ValueError("Invalid cursor context")
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValidationFailedError("Invalid document cursor") from exc
    if payload[3:5] != [fence.membership_revision, fence.configuration_revision]:
        raise ConflictError("Document page context changed")
    return str(payload[6])


async def list_documents(
    session: AsyncSession, limit: int, cursor: str | None, source_id: UUID | None, *,
    scope: Scope, multi_workspace_enabled: bool, exclude_measurements: bool = False,
) -> tuple[list[Document], str | None]:
    """Page <=100 retained owner roots, constraining workspace/Source before limit and cursor.

    Preserve created-time/UUID descending keyset semantics; optional foreign Source yields
    an empty page. Members cannot enumerate IDs/counts. Data-purged Sources are excluded,
    while paused/connector-only archived metadata remains readable. No locks or commit.
    ``exclude_measurements`` hides records from Sources whose provider spec has
    ``data_kind == "measurement"`` (ISS position, prices, weather): they are observations, not
    documents. It only narrows the scoped set, so cursors and paging semantics are unchanged.
    """
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext)):
        raise TypeError("Document list requires a workspace owner")
    fence = await _admit_document_scope(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, allow_member=True)
    if not 1 <= limit <= 100:
        raise ValueError("Document page size must be between 1 and 100")
    statement = select(Document).where(*_document_scope(scope, allow_member=True))
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    if exclude_measurements:
        # Deferred: Connectors' facade imports Documents' contract at module level, so importing it here at module
        # level would form an import cycle.
        from modules.connectors.public import FREE_PROVIDER_SPECS

        measurement_providers = [spec.id for spec in FREE_PROVIDER_SPECS if spec.data_kind == "measurement"]
        statement = statement.where(Document.source_id.not_in(
            select(Source.id).where(Source.provider.in_(measurement_providers))))
    statement = statement.order_by(desc(Document.created_at), desc(Document.id))
    if cursor is not None:
        timestamp, identifier = decode_cursor(_decode_document_owner_cursor(
            cursor, kind="documents", fence=fence, resource_id=source_id))
        statement = statement.where(
            tuple_(Document.created_at, Document.id) < (timestamp, identifier)
        )
    rows = list((await session.scalars(statement.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_document_owner_cursor(encode_cursor(rows[-1].created_at, rows[-1].id),
        kind="documents", fence=fence, resource_id=source_id) if has_more and rows else None
    return rows, next_cursor


async def update_document(
    session: AsyncSession, document_id: UUID, payload: DocumentPatch, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document | None:
    """Patch an exact owner root freshly under Source→Document locks, then scoped replay.

    Never trust a previously read ORM. Retained inactive metadata is eligible except data
    purges. Missing/foreign root returns None; only actual title/metadata changes publish.
    Refresh generated fields before the commit releases original admission/domain locks.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document metadata writes require a workspace owner")
    source_id = await _read_document_source_id(session, document_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if source_id is None:
        return None
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    document = await session.scalar(select(Document).where(
        Document.id == document_id, Document.source_id == source_id, *_document_scope(scope),
    ).with_for_update().execution_options(populate_existing=True))
    if document is None:
        return None
    changed = False
    if "title" in payload.model_fields_set:
        value = payload.title or ""
        changed = changed or document.title != value
        document.title = value
    if "metadata" in payload.model_fields_set:
        metadata_value = payload.metadata or {}
        changed = changed or document.metadata_json != metadata_value
        document.metadata_json = metadata_value
    drafts = [make_knowledge_change(document.source_id, document.id, document.current_version, scope=scope)] if changed else []
    await session.flush()
    await session.refresh(document)
    await commit_with_replay(session, drafts, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=locked.access_fence)
    return document


async def append_content(
    session: AsyncSession, document_id: UUID, expected_version: int, content: str, *,
    scope: WorkspaceContext, multi_workspace_enabled: bool,
) -> Document | None:
    """Append one owner revision under early active Source→Document locks and scoped replay.

    Missing/foreign/data-purged document or inactive Source returns None. Identical content
    is a no-op before expected-version CAS; changed stale content raises ValueError. Allocate
    max historical revision+1 while Source/key absence remains serialized, select it before
    scoped ready events, refresh under held locks and commit once. No external I/O.
    """
    if not isinstance(scope, WorkspaceContext):
        raise TypeError("Document content writes require a workspace owner")
    source_id = await _read_document_source_id(session, document_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if source_id is None:
        return None
    locked = await sources.lock_source_set(session, (source_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    source_fence, access_fence = locked.fences[0], locked.access_fence
    if source_fence.status != "active":
        return None
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.source_id == source_id, *_document_scope(scope))
        .with_for_update().execution_options(populate_existing=True)
    )
    if document is None:
        return None
    current = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == document.current_version,
        )
    )
    if current is None:
        raise RuntimeError("Current document version is missing")
    if current.content == content:
        return document
    # Check the revision only after the identical-content no-op to keep retries idempotent.
    if document.current_version != expected_version:
        raise ValueError("Document revision is stale")
    max_number = await session.scalar(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document.id)
    )
    next_version = int(max_number or 0) + 1
    digest = await content_hash_async(content)
    version = DocumentVersion(
            document_id=document_id,
            version_number=next_version,
            content=content,
            content_hash=digest,
    )
    session.add(version)
    document.current_version = next_version
    document.content_hash = digest
    if await add_content_chunks(session, version):
        await _publish_document_ready(session, document, version, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence)
    await session.flush()
    await session.refresh(document)
    await commit_with_replay(
        session,
        [make_knowledge_change(document.source_id, document.id, next_version, scope=scope)],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    return document


async def count_source_documents(session: AsyncSession, source_id: UUID, *, scope: Scope, cap: int = 1000) -> int:
    """Return an owner-UI-only document count for one source, saturating at ``cap``.

    The caller has admitted ``scope``; the workspace and purge predicate sit inside the capped
    subquery so the cap never counts foreign rows.
    """
    capped = select(Document.id).where(
        Document.source_id == source_id, *_document_scope(scope),
    ).limit(cap).subquery()
    return int(await session.scalar(select(func.count()).select_from(capped)) or 0)


async def purge_expired_document_versions(
    session: AsyncSession,
    cutoffs: Sequence[tuple[UUID, datetime]],
    *,
    limit: int,
    is_protected: Callable[[SQLColumnExpression[Any]], ColumnElement[bool]] | None = None,
) -> int:
    """Delete up to ``limit`` expired non-current document versions; the caller commits.

    A version is kept when it is its document's current version, when any owner interaction
    bookmarks it, or when ``is_protected(DocumentVersion.id)`` is true (ingestion passes the
    Observations "backs a current observation" predicate; injected because Observations public
    already imports this module). Per-source ``cutoffs`` bound ``observed_at``; oldest first with
    ``FOR UPDATE SKIP LOCKED`` on versions so cleanup never blocks ingestion. Returns rows deleted.
    """
    if not cutoffs:
        return 0
    bookmarked = (
        select(1)
        .where(
            DocumentInteraction.document_version_id == DocumentVersion.id,
            DocumentInteraction.bookmarked_at.is_not(None),
        )
        .exists()
    )
    expired = or_(
        *(and_(Document.source_id == sid, DocumentVersion.observed_at < cutoff) for sid, cutoff in cutoffs)
    )
    conditions = [DocumentVersion.version_number != Document.current_version, not_(bookmarked), expired]
    if is_protected is not None:
        conditions.append(not_(is_protected(DocumentVersion.id)))
    doomed = (
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(*conditions)
        .order_by(DocumentVersion.observed_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True, of=DocumentVersion)
    )
    result = await session.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(doomed)))
    return int(getattr(result, "rowcount", 0) or 0)
