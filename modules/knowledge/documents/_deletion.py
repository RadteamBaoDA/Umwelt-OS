"""Document and Source-purge deletion: cleanup closure discovery, locking and graph cleanup."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from sqlalchemy import (
    and_,
    case,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.events import DomainEvent
from core.realtime import ReplayDraft, commit_with_replay, make_knowledge_change
from core.workspaces import public as workspaces
from core.workspaces.admission import actor_id
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._cleanup import capture_document_cleanup_evidence
from modules.knowledge.documents._evidence import _list_evidence_ref_keys
from modules.knowledge.documents._scope import (
    _admit_document_scope,
    _document_scope,
    _read_document_source_id,
)
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupEvidenceReference,
    DocumentCleanupOperation,
    DocumentVersion,
    NormalizedDocumentIdentity,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    DocumentCleanupPreparationLimitError,
)
from modules.sources import public as sources
from modules.sources.schemas import (
    SourceFence,
    SourcePurgeJobIdentity,
)

if TYPE_CHECKING:
    from modules.knowledge.entities.public import EntitySupportClosure
    from modules.knowledge.observations.public import ObservationCleanupClosure
    from modules.knowledge.relationships.public import RelationshipSupportClosure
    from modules.knowledge.temporal.public import TemporalCleanupClosure
    from modules.timeline.public import TimelineSupportClosure

_CLEANUP_LIMIT = 10_000


@dataclass(frozen=True)
class _CleanupClosure:
    """Discovered ID closure of one Document or Source cleanup across Documents and graph owners."""

    source_id: UUID
    document_id: UUID | None
    document_ids: tuple[UUID, ...]
    version_ids: tuple[UUID, ...]
    chunk_ids: tuple[UUID, ...]
    provenance_ids: tuple[UUID, ...]
    identity_ids: tuple[UUID, ...]
    refs: tuple[tuple[UUID, UUID], ...]
    observations: "ObservationCleanupClosure"
    entities: "EntitySupportClosure"
    relationships: "RelationshipSupportClosure"
    timeline: "TimelineSupportClosure"
    temporal: "TemporalCleanupClosure"
    entity_union: tuple[UUID, ...]


async def _require_cleanup_authority(
    session: AsyncSession, *, source_id: UUID, scope: Scope, flag: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Nonlocking proof that the caller-held fences are the current admission and own Source."""
    actual = await _admit_document_scope(session, scope=scope, multi_workspace_enabled=flag)
    if actual != access_fence or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id:
        raise ConflictError("Cleanup authority changed")


async def _bounded_ids(session: AsyncSession, statement: Any) -> tuple[tuple[UUID, ...], bool]:
    """Read at most ``_CLEANUP_LIMIT`` ids of ``statement``; the flag reports that more existed.

    Fetches one extra row to detect overflow without an unbounded read. Read-only; no locks.
    """
    ids = list((await session.scalars(statement.limit(_CLEANUP_LIMIT + 1))).all())
    return tuple(ids[:_CLEANUP_LIMIT]), len(ids) > _CLEANUP_LIMIT


async def _prepare_cleanup_closure(
    session: AsyncSession, *, source_id: UUID, document_id: UUID | None, scope: Scope, flag: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> _CleanupClosure | DocumentCleanupPreparationLimitError:
    """Discover (nonlocking, ID-only) the whole cleanup closure; report, never raise, an owner overflow.

    Documents children first, then Observations, Entities, Relationships, Timeline, Temporal. The
    first overflowed owner in allowlist order is returned as the limit error before any lock.
    """
    from modules.knowledge.entities import public as entities
    from modules.knowledge.observations import public as observations
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    await _require_cleanup_authority(
        session, source_id=source_id, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence,
    )
    where = [Document.workspace_id == scope.workspace_id, Document.source_id == source_id]
    if document_id is not None:
        where.append(Document.id == document_id)
    document_ids, over_documents = await _bounded_ids(
        session, select(Document.id).where(*where).order_by(Document.id))
    version_ids, over_versions = await _bounded_ids(session, select(DocumentVersion.id).join(
        Document, Document.id == DocumentVersion.document_id).where(*where).order_by(DocumentVersion.id))
    chunk_ids, over_chunks = await _bounded_ids(session, select(DocumentChunk.id).join(
        DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id).join(
        Document, Document.id == DocumentVersion.document_id).where(*where).order_by(DocumentChunk.id))
    provenance_ids, over_provenance = await _bounded_ids(session, select(NormalizedVersionProvenance.id).join(
        Document, Document.id == NormalizedVersionProvenance.document_id).where(*where)
        .order_by(NormalizedVersionProvenance.id))
    identity_where = [
        NormalizedDocumentIdentity.workspace_id == scope.workspace_id,
        NormalizedDocumentIdentity.source_id == source_id,
    ]
    if document_id is not None:
        identity_where.append(or_(
            NormalizedDocumentIdentity.document_id == document_id,
            NormalizedDocumentIdentity.external_id.in_(
                select(Document.external_id).where(*where, Document.external_id.is_not(None))),
        ))
    identity_ids, over_identities = await _bounded_ids(
        session, select(NormalizedDocumentIdentity.id).where(*identity_where).order_by(NormalizedDocumentIdentity.id))
    refs, over_refs = await _list_evidence_ref_keys(session, scope=scope, source_id=source_id, document_id=document_id)
    if over_documents or over_versions or over_chunks or over_provenance or over_identities or over_refs:
        return DocumentCleanupPreparationLimitError("documents")
    closure_o = await observations.observation_cleanup_ids(
        session, source_id=source_id, document_id=document_id, scope=scope, multi_workspace_enabled=flag)
    if closure_o.overflow:
        return DocumentCleanupPreparationLimitError("observations")
    closure_e = await entities.support_cleanup_ids(
        session, source_id=source_id, document_id=document_id, scope=scope, multi_workspace_enabled=flag)
    if closure_e.overflow:
        return DocumentCleanupPreparationLimitError("entities")
    closure_r = await relationships.support_cleanup_ids(
        session, refs=refs, source_id=source_id, document_id=document_id,
        membership_ids=closure_e.membership_ids, scope=scope, multi_workspace_enabled=flag)
    if closure_r.overflow:
        return DocumentCleanupPreparationLimitError("relationships")
    closure_t = await timeline.support_cleanup_ids(
        session, source_id=source_id, document_id=document_id, scope=scope, multi_workspace_enabled=flag)
    if closure_t.overflow:
        return DocumentCleanupPreparationLimitError("timeline")
    closure_g = await temporal.tombstone_cleanup_ids(
        session, source_id=source_id, document_id=document_id, scope=scope, multi_workspace_enabled=flag)
    if closure_g.overflow:
        return DocumentCleanupPreparationLimitError("temporal")
    union = tuple(sorted(
        set(closure_e.entity_ids) | set(closure_r.endpoint_entity_ids) | set(closure_t.participant_entity_ids)))
    return _CleanupClosure(
        source_id=source_id, document_id=document_id, document_ids=document_ids, version_ids=version_ids,
        chunk_ids=chunk_ids, provenance_ids=provenance_ids, identity_ids=identity_ids, refs=tuple(refs),
        observations=closure_o, entities=closure_e, relationships=closure_r, timeline=closure_t,
        temporal=closure_g, entity_union=union,
    )


async def _lock_cleanup_closure(
    session: AsyncSession, closure: _CleanupClosure, *, scope: Scope, flag: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Lock the discovered closure in the canonical order; mutation-free.

    Documents, Document children, distinct raw-URI identities, normalized identities, then
    Observations, Entities (combined union), Relationships, Timeline and Temporal.
    """
    from modules.knowledge.entities import public as entities
    from modules.knowledge.observations import public as observations
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    workspace_id = scope.workspace_id
    if closure.document_ids:
        await session.scalars(select(Document.id).where(
            Document.workspace_id == workspace_id, Document.id.in_(closure.document_ids),
        ).order_by(Document.id).with_for_update())
    if closure.version_ids:
        await session.scalars(select(DocumentVersion.id).where(
            DocumentVersion.id.in_(closure.version_ids),
            DocumentVersion.document_id.in_(select(Document.id).where(Document.workspace_id == workspace_id)),
        ).order_by(DocumentVersion.id).with_for_update())
    if closure.chunk_ids:
        await session.scalars(select(DocumentChunk.id).where(
            DocumentChunk.id.in_(closure.chunk_ids),
            DocumentChunk.document_version_id.in_(select(DocumentVersion.id).join(
                Document, Document.id == DocumentVersion.document_id).where(Document.workspace_id == workspace_id)),
        ).order_by(DocumentChunk.id).with_for_update())
    if closure.provenance_ids:
        await session.scalars(select(NormalizedVersionProvenance.id).where(
            NormalizedVersionProvenance.id.in_(closure.provenance_ids),
            NormalizedVersionProvenance.document_id.in_(
                select(Document.id).where(Document.workspace_id == workspace_id)),
        ).order_by(NormalizedVersionProvenance.id).with_for_update())
    # Materialize and order identities in PostgreSQL; the lock calls do not emit URI values to Python.
    scoped_document = " AND id = :document_id" if closure.document_id is not None else ""
    parameters: dict[str, Any] = {"workspace_id": workspace_id, "source_id": closure.source_id}
    if closure.document_id is not None:
        parameters["document_id"] = closure.document_id
    await session.execute(text(
        "WITH identities AS MATERIALIZED ("
        " SELECT DISTINCT raw_uri FROM documents"
        " WHERE workspace_id = :workspace_id AND source_id = :source_id"
        f"{scoped_document} AND raw_uri IS NOT NULL AND raw_uri <> ''"
        " ORDER BY raw_uri"
        "), locks AS MATERIALIZED ("
        " SELECT pg_advisory_xact_lock(hashtextextended('documents.raw:' || raw_uri, 0)) AS acquired"
        " FROM identities ORDER BY raw_uri"
        ") SELECT count(*) FROM locks"
    ), parameters)
    if closure.identity_ids:
        await session.scalars(select(NormalizedDocumentIdentity.id).where(
            NormalizedDocumentIdentity.workspace_id == workspace_id,
            NormalizedDocumentIdentity.id.in_(closure.identity_ids),
        ).order_by(NormalizedDocumentIdentity.id).with_for_update())
    await observations.prepare_document_cleanup_in_uow(
        session, closure.observations, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await entities.prepare_support_cleanup_in_uow(
        session, closure.entities, entity_ids=closure.entity_union, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await relationships.prepare_support_cleanup_in_uow(
        session, closure.relationships, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await timeline.prepare_support_cleanup_in_uow(
        session, closure.timeline, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await temporal.prepare_tombstone_scope_in_uow(
        session, closure.temporal, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)


async def _apply_graph_cleanup(
    session: AsyncSession, closure: _CleanupClosure, *, scope: Scope, flag: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> list[ReplayDraft]:
    """Apply the prepared graph/timeline cleanup under held locks; returns timeline replay drafts.

    Temporal tombstone first (detached identity before any cascade), relationship history
    support is stripped before relationship/entity/timeline support is removed. A changed or
    overflowed closure raises inside the owners and aborts the whole transaction.
    """
    from modules.knowledge.entities import public as entities
    from modules.knowledge.relationships import public as relationships
    from modules.knowledge.temporal import public as temporal
    from modules.timeline import public as timeline

    refs = list(closure.refs)
    await temporal.tombstone_scope_in_uow(
        session, closure.temporal, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await relationships.purge_history_support(
        session, closure.relationships, refs, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    if closure.document_id is not None:
        await relationships.remove_document_support(
            session, closure.relationships, refs=refs, membership_ids=closure.entities.membership_ids,
            scope=scope, multi_workspace_enabled=flag, access_fence=access_fence, source_fence=source_fence)
        await entities.remove_document_support(
            session, closure.entities, scope=scope, multi_workspace_enabled=flag,
            access_fence=access_fence, source_fence=source_fence)
        return await timeline.remove_document_support(
            session, closure.timeline, scope=scope, multi_workspace_enabled=flag,
            access_fence=access_fence, source_fence=source_fence)
    await relationships.remove_source_support(
        session, closure.relationships, refs=refs, membership_ids=closure.entities.membership_ids,
        scope=scope, multi_workspace_enabled=flag, access_fence=access_fence, source_fence=source_fence)
    await entities.remove_source_support(
        session, closure.entities, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    return await timeline.remove_source_support(
        session, closure.timeline, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)


def _new_cleanup_operation(
    document: Document, *, scope: Scope, source_id: UUID, access_fence: AccessFence,
    source_fence: SourceFence,
) -> DocumentCleanupOperation:
    """Build the (unflushed) queued cleanup receipt for one document deletion.

    Captures the admitted membership/configuration epochs and Source generation so the later
    asynchronous stages can re-prove authority. Pure construction: no session access.
    """
    return DocumentCleanupOperation(
        workspace_id=scope.workspace_id,
        actor_user_id=actor_id(scope),
        membership_revision=access_fence.membership_revision,
        configuration_revision=access_fence.configuration_revision,
        source_generation=source_fence.generation,
        source_id=source_id,
        document_id=document.id,
        raw_uri=document.raw_uri,
        raw_status="queued" if document.raw_uri else "not_present",
        evidence_scope_status="capturing",
        copied_status="queued",
        chat_status="queued",
        status="queued",
    )


async def _tombstone_normalized_identity(
    session: AsyncSession, document: Document, *, source_id: UUID, scope: Scope,
) -> None:
    """Tombstone the normalized (source, external_id) identity of a document being deleted.

    No-op for documents without an ``external_id``. The identity row was locked by the cleanup
    closure; a missing row is created under the held Source lock, flushed, and then detached from
    the document with ``tombstoned_at`` set. Runs inside the caller's transaction (no commit).
    """
    if document.external_id is None:
        return
    doc_identity = await session.scalar(select(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.workspace_id == scope.workspace_id,
        NormalizedDocumentIdentity.source_id == source_id,
        NormalizedDocumentIdentity.external_id == document.external_id,
    ))  # rows were locked by the closure; a missing row is created under the held Source lock
    if doc_identity is None:
        doc_identity = NormalizedDocumentIdentity(
            workspace_id=scope.workspace_id,
            source_id=source_id,
            external_id=document.external_id,
            document_id=document.id,
        )
    session.add(doc_identity)
    await session.flush()
    doc_identity.tombstoned_at = datetime.now(UTC)
    doc_identity.document_id = None


async def delete_document(
    session: AsyncSession, document_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> DocumentCleanupOperation | None:
    """Commit access revocation, detached evidence IDs, and durable asynchronous cleanup stages.

    The route already holds the exact access fence. Lock order is access fence, Source, Document,
    then the discovered closure (Document children, raw-URI identity, normalized identity,
    Observations, Entities, Relationships, Timeline, Temporal, Ingestion). A dependency-limit
    error is raised before any lock or effect. The receipt captures the admitted epochs and
    Source generation; its operation-only event and the deletion/tombstone replay commit
    atomically. The caller owns authorization; a successful return means canonical access is
    revoked, not that any cleanup owner stage has completed.
    """
    from modules.ingestion import public as ingestion
    from modules.knowledge.observations import public as observations

    flag = multi_workspace_enabled
    source_id = await _read_document_source_id(session, document_id, scope=scope, multi_workspace_enabled=flag)
    if source_id is None:
        return None
    locked = await sources.lock_source_set(session, (source_id,), scope=scope, multi_workspace_enabled=flag)
    source_fence, access_fence = locked.fences[0], locked.access_fence
    document = await session.scalar(select(Document).where(
        Document.id == document_id, Document.source_id == source_id, *_document_scope(scope),
    ).with_for_update().execution_options(populate_existing=True))
    if document is None:
        return None
    closure = await _prepare_cleanup_closure(
        session, source_id=source_id, document_id=document_id, scope=scope, flag=flag,
        access_fence=access_fence, source_fence=source_fence)
    if isinstance(closure, DocumentCleanupPreparationLimitError):
        raise closure
    await _lock_cleanup_closure(
        session, closure, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence)
    await ingestion.prepare_document_materializations_in_uow(
        session, document_id, source_id=source_id, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    operation = _new_cleanup_operation(
        document, scope=scope, source_id=source_id, access_fence=access_fence, source_fence=source_fence)
    session.add(operation)
    await session.flush()
    await capture_document_cleanup_evidence(session, operation)
    operation.evidence_scope_status = "captured"
    await ingestion.publish_event(session, DomainEvent(
        id=uuid5(operation.id, "document-cleanup-requested"),
        type="document.cleanup.requested",
        version=1,
        occurred_at=datetime.now(UTC),
        producer="modules.knowledge.documents",
        payload={"operation_id": str(operation.id)},
    ), scope=scope, multi_workspace_enabled=flag)
    await observations.purge_document_in_uow(
        session, document.id, source_id=source_id, closure=closure.observations, scope=scope,
        multi_workspace_enabled=flag, access_fence=access_fence, source_fence=source_fence)
    await _tombstone_normalized_identity(session, document, source_id=source_id, scope=scope)
    await ingestion.tombstone_document_materializations(
        session, document.id, scope=scope, multi_workspace_enabled=flag)
    timeline_drafts = await _apply_graph_cleanup(
        session, closure, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence)
    deleted = (await session.scalars(
        delete(Document)
        .where(Document.id == document_id, Document.source_id == source_id,
               Document.workspace_id == scope.workspace_id)
        .returning(Document.id)
    )).first() is not None
    if not deleted:
        raise RuntimeError("Locked document disappeared during its cleanup transaction")
    await workspaces.revoke_resource_shares_in_uow(
        session, workspace_id=scope.workspace_id, resource_type="document", resource_ids=(document_id,))
    drafts: list[ReplayDraft] = [
        *timeline_drafts, make_knowledge_change(source_id, document_id, deleted=True, scope=scope),
    ]
    await commit_with_replay(
        session, drafts, scope=scope, multi_workspace_enabled=flag, access_fence=access_fence)
    await session.refresh(operation)
    return operation


async def _capture_source_document_cleanup(
    session: AsyncSession, source_id: UUID, source_purge_operation_id: UUID, *,
    capture: SourcePurgeJobIdentity, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Create one durable cleanup receipt and exact evidence children for every source document.

    The caller owns the Source lock, transaction, and already-locked Documents set (raw-URI
    identities were locked by ``lock_source_documents_for_purge_in_uow``; no lock is taken here).
    Owner-local INSERT SELECT statements write the NOT NULL workspace/actor identity and the
    captured membership/configuration/Source-generation epochs, so versions, chunks and URIs are
    never assembled into an unbounded Python snapshot. Child events commit with the canonical cascade.
    Receipt and child ids use the in-database ``uuid_generate_v7()`` so each selected row gets its own UUIDv7.
    """
    workspace_id = scope.workspace_id
    await session.execute(insert(DocumentCleanupOperation).from_select(
        [
            "id", "workspace_id", "actor_user_id", "membership_revision", "configuration_revision",
            "source_generation", "source_id", "document_id", "source_purge_operation_id", "raw_uri",
            "record_status", "graph_status", "raw_status", "evidence_scope_status",
            "copied_status", "chat_status", "status", "earliest_version_created_at",
        ],
        select(
            func.uuid_generate_v7(), literal(capture.workspace_id), literal(capture.actor_user_id),
            literal(capture.membership_revision), literal(capture.configuration_revision),
            literal(capture.source_generation), Document.source_id, Document.id,
            literal(source_purge_operation_id), Document.raw_uri,
            literal("deleted"), literal("tombstoned"),
            case(((Document.raw_uri.is_not(None) & (Document.raw_uri != "")), "queued"), else_="not_present"),
            literal("capturing"), literal("queued"), literal("queued"), literal("queued"),
            select(func.min(DocumentVersion.created_at))
            .where(DocumentVersion.document_id == Document.id).scalar_subquery(),
        ).where(Document.workspace_id == workspace_id, Document.source_id == source_id),
    ))

    columns = ["id", "workspace_id", "operation_id", "document_version_id", "chunk_id", "reference_kind"]
    receipt_scope = and_(
        DocumentCleanupOperation.workspace_id == workspace_id,
        DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
        DocumentCleanupOperation.document_id == DocumentVersion.document_id,
    )
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        columns,
        select(
            func.uuid_generate_v7(), DocumentCleanupOperation.workspace_id, DocumentCleanupOperation.id,
            DocumentVersion.id, literal(None), literal("version"),
        ).join(DocumentCleanupOperation, receipt_scope),
    ))
    await session.execute(insert(DocumentCleanupEvidenceReference).from_select(
        columns,
        select(
            func.uuid_generate_v7(), DocumentCleanupOperation.workspace_id, DocumentCleanupOperation.id,
            DocumentChunk.document_version_id, DocumentChunk.id, literal("chunk"),
        ).join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(DocumentCleanupOperation, receipt_scope),
    ))
    await session.execute(update(DocumentCleanupOperation).where(
        DocumentCleanupOperation.workspace_id == workspace_id,
        DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
    ).values(evidence_scope_status="captured"))

    # At most 10,000 receipts exist by the gate above; keyset them to bound event batches.
    from modules.ingestion import public as ingestion

    after: UUID | None = None
    while True:
        statement = select(DocumentCleanupOperation.id).where(
            DocumentCleanupOperation.workspace_id == workspace_id,
            DocumentCleanupOperation.source_purge_operation_id == source_purge_operation_id,
        )
        if after is not None:
            statement = statement.where(DocumentCleanupOperation.id > after)
        receipt_ids = list((await session.scalars(
            statement.order_by(DocumentCleanupOperation.id).limit(500)
        )).all())
        if not receipt_ids:
            break
        now = datetime.now(UTC)
        for receipt_id in receipt_ids:
            await ingestion.publish_event(session, DomainEvent(
                id=uuid5(receipt_id, "document-cleanup-requested"),
                type="document.cleanup.requested",
                version=1,
                occurred_at=now,
                producer="modules.knowledge.documents",
                payload={"operation_id": str(receipt_id)},
            ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        after = receipt_ids[-1]


async def lock_source_documents_for_purge_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Early Source-purge preparation: lock the whole Documents/graph closure after the Source lock.

    Nonlocking fence proof, bounded discovery (more than 10,000 Documents or any owner overflow
    returns None so the late delete reports the exact limit), then the canonical lock phase. No
    mutation, receipt or event.
    """
    flag = multi_workspace_enabled
    closure = await _prepare_cleanup_closure(
        session, source_id=source_id, document_id=None, scope=scope, flag=flag,
        access_fence=access_fence, source_fence=source_fence)
    if isinstance(closure, DocumentCleanupPreparationLimitError):
        return
    await _lock_cleanup_closure(
        session, closure, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence)


async def delete_source_documents_in_uow(
    session: AsyncSession, source_id: UUID, *, source_purge_operation_id: UUID, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> list[ReplayDraft]:
    """Capture exact cleanup children, then delete source-owned canonical data under held locks.

    Late phase: the Source row, retained operation and outbox union are locked and the closure was
    locked early. The retained capture must equal the held fences. More than 10,000 Documents
    raises the exact atomic-limit ValueError and an owner overflow raises the preparation limit
    error, both before any effect. No lock is taken here; no filesystem or cross-owner I/O occurs.
    The closure is rediscovered here (the early closure cannot be carried through the frozen
    signature), so the owners' held-apply "closure changed" compare is against this late
    rediscovery; equality with the early lock set relies on the held Source lock serializing writers.
    """
    from modules.knowledge.observations import public as observations

    flag = multi_workspace_enabled
    await _require_cleanup_authority(
        session, source_id=source_id, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence)
    capture = await sources.read_source_purge_job_capture(
        session, source_purge_operation_id, scope=scope, multi_workspace_enabled=flag)
    if (capture is None or capture.membership_revision != access_fence.membership_revision
            or capture.configuration_revision != access_fence.configuration_revision
            or capture.source_generation != source_fence.generation or capture.source_id != source_id):
        raise RuntimeError("Source purge authority differs from the held cleanup fences")
    document_ids = list((await session.scalars(select(Document.id).where(
        Document.workspace_id == scope.workspace_id, Document.source_id == source_id,
    ).order_by(Document.id).limit(_CLEANUP_LIMIT + 1))).all())
    if len(document_ids) > _CLEANUP_LIMIT:
        raise ValueError("Source graph cleanup exceeds its atomic document limit")
    closure = await _prepare_cleanup_closure(
        session, source_id=source_id, document_id=None, scope=scope, flag=flag,
        access_fence=access_fence, source_fence=source_fence)
    if isinstance(closure, DocumentCleanupPreparationLimitError):
        raise closure
    await _capture_source_document_cleanup(
        session, source_id, source_purge_operation_id, capture=capture, scope=scope, multi_workspace_enabled=flag)
    await observations.purge_source_in_uow(
        session, source_id, closure=closure.observations, scope=scope, multi_workspace_enabled=flag,
        access_fence=access_fence, source_fence=source_fence)
    await session.execute(delete(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.workspace_id == scope.workspace_id,
        NormalizedDocumentIdentity.source_id == source_id,
    ))
    timeline_drafts = await _apply_graph_cleanup(
        session, closure, scope=scope, flag=flag, access_fence=access_fence, source_fence=source_fence)
    await session.execute(delete(Document).where(
        Document.workspace_id == scope.workspace_id, Document.source_id == source_id))
    # Workspace-wide revoke (never actor-bound): document_ids is the locked Source-wide set.
    for start in range(0, len(document_ids), 1000):
        await workspaces.revoke_resource_shares_in_uow(
            session, workspace_id=scope.workspace_id, resource_type="document",
            resource_ids=tuple(document_ids[start:start + 1000]))
    return timeline_drafts
