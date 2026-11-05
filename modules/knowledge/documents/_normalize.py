"""Validation-first normalized upserts: immutable revisions, ranking and World selection."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    and_,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from core.workspaces import public as workspaces
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._chunks import _superseded_version_hooks, add_content_chunks
from modules.knowledge.documents._hashing import content_hash, content_hash_async
from modules.knowledge.documents._identity import (
    NormalizedDocumentValidationRejected,
    _normalized_key_states,
    _normalized_source_proof,
    _normalized_target,
    prepare_normalized_document_keys,
)
from modules.knowledge.documents._language import _provenance_language
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    NormalizedDocumentIdentity,
    NormalizedVersionProvenance,
)
from modules.knowledge.documents.schemas import (
    MEASUREMENT_PROVIDER_IDS,
    NormalizedDocumentInput,
    NormalizedDocumentKeyState,
    NormalizedDocumentPreparation,
    NormalizedDocumentResult,
    ProviderRecordMetadata,
)
from modules.sources import public as sources
from modules.sources.schemas import (
    ConnectorSource,
    SourceFence,
)


async def ensure_demo_article(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int, int]:
    """Normalize one fictional article through Documents and preserve provenance and chunks.

    Returns (created, existing, skipped). The stable source/provider identity and accepted content
    hash make retries idempotent; a tombstoned normalized identity stays deleted. The caller owns
    the transaction and receipt, while Documents owns source fencing, normalization, and chunking.
    The finite caller supplies real scope/configured flag; no demo owner/default epoch is inferred.
    """
    from core.demo_seed import P12_DEMO_NAMESPACE, p12_demo_seed_id

    article_id = p12_demo_seed_id("article", "lantern-inscription-care")
    source_id = p12_demo_seed_id("article", "source")
    content = (
        "Fictional field note: Mira records that the north orchard lantern inscriptions should be "
        "photographed in soft morning light before the catalogue is assembled."
    )
    await sources.ensure_demo_source(session, source_id, P12_DEMO_NAMESPACE,
                                    scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source = await sources.get_source_fence(session, source_id, scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise RuntimeError("P12 demo article source is unavailable")
    title = "Field note: caring for orchard lantern inscriptions"
    result = await upsert_normalized_document(session, NormalizedDocumentInput(
        source_id=source_id,
        expected_source_generation=source.generation,
        observation_id=article_id,
        provider_id=f"{P12_DEMO_NAMESPACE}/lantern-inscription-care",
        accepted_record_hash=content_hash(content),
        normalization_version=1,
        observed_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        title=title,
        canonical_url="https://example.invalid/demo/orchard-lantern-care",
        content_type="article",
        content=content,
        provenance={"title": title, "content_type": "article"},
    ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if result.disposition == "tombstoned":
        return 0, 0, 1
    return (1, 0, 0) if result.created_version else (0, 1, 0)


async def upsert_normalized_document(
    session: AsyncSession, payload: NormalizedDocumentInput, *, scope: Scope, multi_workspace_enabled: bool,
) -> NormalizedDocumentResult:
    """Acquire Source then sorted Document/identity, retaining the ordinary result.

    Entry has no later domain/outbox locks. The owner captures current fences for
    this immediate operation and shares the held mutation body; caller commits.
    Deferred ingestion uses its original fences and explicit preparation instead.
    """
    source_fence = await sources.lock_source(
        session, payload.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source_fence is None or source_fence.status != "active"
            or source_fence.generation != payload.expected_source_generation):
        raise ValueError("Normalized source generation is no longer active")
    access_fence = await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preparation = await prepare_normalized_document_keys(
        session, payload.source_id, (payload.provider_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    result, _ = await upsert_normalized_document_in_uow(
        session, payload, preparation=preparation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    return result


async def upsert_normalized_document_in_uow(
    session: AsyncSession, payload: NormalizedDocumentInput, *, preparation: NormalizedDocumentPreparation,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> tuple[NormalizedDocumentResult, NormalizedDocumentPreparation]:
    """Apply one prepared held key and return only its owner-produced successor.

    Actual admission/Source/Documents/identity locks remain held in this same
    transaction. Compare original full fences and exact current identity before
    writes; no FOR UPDATE, acquiring wrapper or ON CONFLICT winner adoption.
    Local inserted IDs are verified after flush and replace only this key. Unique
    failures and consistency conflicts abort the complete attempt; never commit.
    Explicit NormalizedDocumentValidationRejected guarantees this record has made no
    owner writes or successor changes. Other exceptions offer no such guarantee.
    """
    from modules.connectors import public as connectors

    try:
        payload = NormalizedDocumentInput.model_validate(payload.model_dump(mode="python"))
    except (ValueError, TypeError) as exc:
        raise NormalizedDocumentValidationRejected("Normalized input validation rejected") from exc
    if (not isinstance(preparation, NormalizedDocumentPreparation)
            or preparation.workspace_id != scope.workspace_id or preparation.source_id != payload.source_id
            or preparation.source_generation != payload.expected_source_generation):
        raise RuntimeError("normalized_preparation_header_changed")
    source_projection = await _normalized_source_proof(
        session, payload.source_id, payload.expected_source_generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    discriminator = payload.provenance.get("provider_scope_discriminator")
    if discriminator is not None or source_projection.provider in MEASUREMENT_PROVIDER_IDS:
        provider_scope = await connectors.get_current_provider_scope(
            session, payload.source_id, payload.expected_source_generation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        # Generic RSS/web/REST sources have no catalog provider on the row; Connectors derives their scope
        # provider from the source type, so compare against the same derivation or every generic fetch is rejected.
        expected_provider = source_projection.provider or {"rss": "rss", "web": "web", "api": "rest"}.get(source_projection.type)
        if (provider_scope is None or provider_scope.workspace_id != scope.workspace_id
                or provider_scope.source_id != payload.source_id
                or provider_scope.source_generation != payload.expected_source_generation
                or provider_scope.provider_id != expected_provider
                or provider_scope.discriminator != discriminator):
            raise RuntimeError("normalized_preparation_provider_changed")
    expected, document, identity = await _normalized_target(session, payload.provider_id, preparation)
    result, document, identity = await _apply_normalized_document(
        session, payload, source_projection, document, identity, preparation.workspace_id,
    )
    state = NormalizedDocumentKeyState(
        external_id=expected.external_id, document_id=document.id if document is not None else None,
        normalized_identity_id=identity.id if identity is not None else None,
        identity_document_id=identity.document_id if identity is not None else None,
        tombstoned_at=identity.tombstoned_at if identity is not None else None,
    )
    fresh, _, _ = await _normalized_key_states(session, payload.source_id, (payload.provider_id,), scope.workspace_id)
    if fresh != (state,):
        raise RuntimeError("normalized_preparation_local_write_changed")
    successor = NormalizedDocumentPreparation(
        workspace_id=preparation.workspace_id, source_id=preparation.source_id,
        source_generation=preparation.source_generation,
        keys=tuple(state if key.external_id == state.external_id else key for key in preparation.keys),
    )
    return result, successor


def _validate_incoming_provenance(
    payload: NormalizedDocumentInput, source_projection: ConnectorSource,
) -> Any:
    """Validate the incoming provider provenance against the immutable Source provider.

    Returns the raw ``provider_record`` provenance value (``None`` when absent). Raises
    ``NormalizedDocumentValidationRejected`` for malformed provenance, a provider mismatch, or a
    Telegram record without delivery provenance. Pure: no session access, so it always precedes
    the record's first owner mutation.
    """
    provider_record = payload.provenance.get("provider_record")
    if provider_record is not None:
        try:
            typed_provider = ProviderRecordMetadata.model_validate(provider_record)
        except (ValueError, TypeError) as exc:
            raise NormalizedDocumentValidationRejected("Incoming provider provenance rejected") from exc
        if source_projection.provider != typed_provider.provider:
            raise NormalizedDocumentValidationRejected("Provider provenance does not match the immutable source provider")
    elif source_projection.provider == "telegram":
        raise NormalizedDocumentValidationRejected("Telegram normalization requires immutable delivery provenance")
    return provider_record


async def _duplicate_normalized_result(
    session: AsyncSession, payload: NormalizedDocumentInput, document: Document,
) -> NormalizedDocumentResult | None:
    """Return the ``duplicate`` result when this accepted hash/normalization version exists.

    Reads (never writes) the prior provenance row of ``document``; ``None`` means the revision is
    new. A provenance row pointing at a missing revision or another provider is a consistency
    error (``RuntimeError``) that aborts the transaction.
    """
    prior = await session.scalar(
        select(NormalizedVersionProvenance).where(
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.accepted_record_hash == payload.accepted_record_hash,
            NormalizedVersionProvenance.normalization_version == payload.normalization_version,
        )
    )
    if prior is None:
        return None
    version = await session.scalar(select(DocumentVersion).where(
        DocumentVersion.id == prior.document_version_id,
        DocumentVersion.document_id == document.id,
    ).execution_options(populate_existing=True))
    if version is None or prior.provider_id != payload.provider_id:
        raise RuntimeError("Normalized provenance references a missing revision")
    count = await session.scalar(
        select(func.count()).select_from(DocumentChunk)
        .where(DocumentChunk.document_version_id == version.id)
    )
    return NormalizedDocumentResult(
        disposition="duplicate", document_id=document.id, document_version_id=version.id,
        version_number=version.version_number, created_version=False,
        selected_current=document.current_version == version.version_number,
        chunk_count=int(count or 0),
    )


async def _current_normalized_provenance(
    session: AsyncSession, document: Document | None,
) -> NormalizedVersionProvenance | None:
    """Load the provenance of the document's current revision, proving it is provider-authored.

    ``None`` for a new document or one without a current revision. An owner-authored current
    revision (no provenance row) conflicts with the provider identity and is rejected.
    """
    if document is None or not document.current_version:
        return None
    current_provenance = await session.scalar(
        select(NormalizedVersionProvenance)
        .join(DocumentVersion, DocumentVersion.id == NormalizedVersionProvenance.document_version_id)
        .where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version_number == document.current_version,
        )
    )
    if current_provenance is None:
        raise NormalizedDocumentValidationRejected("Provider identity conflicts with an owner-authored current revision")
    return current_provenance


def _telegram_selects_incoming(
    payload: NormalizedDocumentInput, provider_record: Any,
    current_provenance: NormalizedVersionProvenance | None,
) -> bool:
    """Rank an incoming Telegram delivery against the current revision by exact delivery order.

    Orders by ``(observed_at, epoch, update_id)`` within one bot binding. Missing ordering proof,
    a different bot, or an equal rank (conflicting content or a different normalization version)
    raise ``NormalizedDocumentValidationRejected``. Pure: no session access.
    """
    try:
        incoming_metadata = ProviderRecordMetadata.model_validate(provider_record)
    except (ValueError, TypeError) as exc:
        raise NormalizedDocumentValidationRejected("Incoming Telegram provenance rejected") from exc
    incoming_telegram = incoming_metadata.telegram
    if incoming_telegram is None or payload.telegram_order is None:
        raise NormalizedDocumentValidationRejected("Telegram version ordering proof is missing")
    incoming_rank = (payload.observed_at, payload.telegram_order.epoch, payload.telegram_order.update_id)
    current_rank = None
    if current_provenance is not None:
        current_metadata = ProviderRecordMetadata.model_validate(
            current_provenance.provenance_json.get("provider_record")
        )
        current_telegram = current_metadata.telegram
        if current_telegram is None or current_telegram.bot_id != incoming_telegram.bot_id:
            raise NormalizedDocumentValidationRejected("Telegram current version has an incompatible bot binding")
        current_rank = (
            current_provenance.selection_observed_at,
            current_telegram.epoch,
            current_telegram.update_id,
        )
    if current_rank == incoming_rank and current_provenance is not None:
        if current_provenance.accepted_record_hash != payload.accepted_record_hash:
            raise NormalizedDocumentValidationRejected("Telegram delivery order has conflicting immutable content")
        raise NormalizedDocumentValidationRejected("Telegram delivery proof already exists with a different normalization version")
    return current_rank is None or incoming_rank > current_rank


def _incoming_selected_current(
    payload: NormalizedDocumentInput, source_projection: ConnectorSource, provider_record: Any,
    current_provenance: NormalizedVersionProvenance | None,
) -> bool:
    """Decide whether the incoming revision becomes the document's current revision.

    Measurement providers never select here (Observation acceptance owns structured-series
    selection); Telegram ranks by delivery order; every other provider by
    ``(observed_at, accepted_record_hash)``. Pure: no session access.
    """
    if source_projection.provider in MEASUREMENT_PROVIDER_IDS:
        # Observation acceptance time and ingestion identity, not provider event
        # time or worker arrival order, own structured-series current selection.
        return False
    if source_projection.provider == "telegram":
        return _telegram_selects_incoming(payload, provider_record, current_provenance)
    current_hash_rank = (
        (current_provenance.selection_observed_at, current_provenance.accepted_record_hash)
        if current_provenance is not None else None
    )
    return current_hash_rank is None or (payload.observed_at, payload.accepted_record_hash) > current_hash_rank


def _select_incoming_as_current(
    document: Document, version: DocumentVersion, payload: NormalizedDocumentInput,
) -> None:
    """Copy the selected revision's denormalized fields onto ``document`` (in-memory mutation).

    The caller flushes; superseded-version hooks run separately before this mutation.
    """
    document.current_version = version.version_number
    document.content_hash = version.content_hash
    document.title = payload.title
    document.canonical_url = payload.canonical_url
    document.published_at = payload.published_at
    document.content_type = payload.content_type
    document.observed_at = payload.observed_at
    document.language = _provenance_language(payload.provenance)
    document.extraction_status = "ready"


async def _write_normalized_revision(
    session: AsyncSession, payload: NormalizedDocumentInput, document: Document, version: DocumentVersion,
    selected: bool,
) -> int:
    """Persist the revision's provenance and chunks and, when selected, switch the current revision.

    Runs inside the caller's transaction (flushes only, no commit). Chunk validation failures are
    reported as ``RuntimeError("normalized_preparation_chunk_write_conflict")``. When a prior
    current revision is superseded, registered hooks run in the same transaction so superseded
    chunks never stay searchable. Returns the number of chunks written.
    """
    session.add(NormalizedVersionProvenance(
        document_id=document.id, document_version_id=version.id,
        provider_id=payload.provider_id, provider_version=payload.provider_version,
        accepted_record_hash=payload.accepted_record_hash,
        normalization_version=payload.normalization_version,
        source_generation=payload.expected_source_generation,
        observed_at=payload.observed_at, received_at=payload.received_at,
        collected_at=payload.collected_at, selection_observed_at=payload.observed_at,
        title=payload.title, canonical_url=payload.canonical_url,
        published_at=payload.published_at, content_type=payload.content_type,
        provenance_json=payload.provenance,
    ))
    try:
        chunk_count = await add_content_chunks(session, version)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("normalized_preparation_chunk_write_conflict") from exc
    if selected:
        previous_version_id = await session.scalar(select(DocumentVersion.id).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.version_number == document.current_version,
        ))
        _select_incoming_as_current(document, version, payload)
        if previous_version_id is not None:
            # Same transaction: superseded chunks are never searchable, so drop derived embeddings.
            for hook in _superseded_version_hooks:
                await hook(session, previous_version_id)
    await session.flush()
    return chunk_count


async def _apply_normalized_document(
    session: AsyncSession, payload: NormalizedDocumentInput, source_projection: ConnectorSource,
    document: Document | None, identity: NormalizedDocumentIdentity | None, workspace_id: UUID,
) -> tuple[NormalizedDocumentResult, Document | None, NormalizedDocumentIdentity | None]:
    """Share validation-first immutable revisions and provider ranking under held roots.

    Preserve tombstones, accepted-hash/version duplicates, generic and exact
    Telegram ordering, monotonic versions and World current selection ownership.
    Known malformed/colliding input emits NormalizedDocumentValidationRejected only
    before this record's first owner mutation. Unknown validation, consistency and
    all later database errors abort the transaction. Insert only proven absences.
    """
    provider_record = _validate_incoming_provenance(payload, source_projection)

    if identity is not None and identity.tombstoned_at is not None:
        return NormalizedDocumentResult(
            disposition="tombstoned", document_id=None, document_version_id=None,
            version_number=None, created_version=False, selected_current=False, chunk_count=0,
        ), document, identity
    if document is not None and (identity is None or identity.document_id is None):
        raise NormalizedDocumentValidationRejected("Provider identity conflicts with an existing non-normalized document")
    if document is not None:
        duplicate = await _duplicate_normalized_result(session, payload, document)
        if duplicate is not None:
            return duplicate, document, identity

    current_provenance = await _current_normalized_provenance(session, document)
    selected = _incoming_selected_current(payload, source_projection, provider_record, current_provenance)
    # All provider/content errors above precede the first owner mutation.
    payload_digest = await content_hash_async(payload.content)  # pure hash, before any session.add
    created_identity_id, created_document_id = None, None
    if identity is None:
        created_identity_id = uuid7()
        identity = NormalizedDocumentIdentity(id=created_identity_id, workspace_id=workspace_id,
                                              source_id=payload.source_id, external_id=payload.provider_id)
        session.add(identity)
    if document is None:
        created_document_id = uuid7()
        document = Document(
            id=created_document_id, workspace_id=workspace_id, source_id=payload.source_id, external_id=payload.provider_id,
            title=payload.title, content_type=payload.content_type,
            canonical_url=payload.canonical_url, published_at=payload.published_at,
            observed_at=payload.observed_at, current_version=0,
            content_hash=payload_digest, extraction_status="ready",
            language=_provenance_language(payload.provenance),
        )
        session.add(document)
        identity.document_id = document.id
        await session.flush()
    max_number = await session.scalar(
        select(func.coalesce(func.max(DocumentVersion.version_number), 0))
        .where(DocumentVersion.document_id == document.id)
    )
    version = DocumentVersion(
        document_id=document.id, version_number=int(max_number or 0) + 1,
        content=payload.content, content_hash=payload_digest,
        observed_at=payload.observed_at,
    )
    session.add(version)
    await session.flush()
    chunk_count = await _write_normalized_revision(session, payload, document, version, selected)
    if (document.workspace_id != workspace_id or identity.workspace_id != workspace_id
            or document.source_id != payload.source_id or identity.source_id != payload.source_id
            or document.external_id != payload.provider_id or identity.external_id != payload.provider_id
            or identity.document_id != document.id or identity.tombstoned_at is not None
            or (created_document_id is not None and document.id != created_document_id)
            or (created_identity_id is not None and identity.id != created_identity_id)):
        raise RuntimeError("normalized_preparation_insert_changed")
    return NormalizedDocumentResult(
        disposition="normalized", document_id=document.id,
        document_version_id=version.id, version_number=version.version_number,
        created_version=True, selected_current=selected, chunk_count=chunk_count,
    ), document, identity


async def select_current_world_document_version(
    session: AsyncSession, *, document_id: UUID, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Acquire Source/Document/identity in order and preserve ordinary bool selection.

    Entry precedes later domain locks. Only an admitted exact workspace document
    can supply the Source/key. The held selector shares pointer mutation while
    deferred callers supply their own original fences and latest preparation.
    Missing source, document or exact selectable version returns False; no commit.
    """
    await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    locator = (await session.execute(select(Document.source_id, Document.external_id).where(
        Document.id == document_id, Document.workspace_id == scope.workspace_id,
    ))).one_or_none()
    if locator is None or locator.external_id is None:
        return False
    source_fence = await sources.lock_source(
        session, locator.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source_fence is None or source_fence.status != "active"
            or source_fence.generation != expected_source_generation):
        return False
    access_fence = await workspaces.read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    preparation = await prepare_normalized_document_keys(
        session, locator.source_id, (locator.external_id,), scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    return await select_current_world_document_version_in_uow(
        session, document_id=document_id, document_version_id=document_version_id,
        expected_source_generation=expected_source_generation,
        provider_scope_discriminator=provider_scope_discriminator, preparation=preparation,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )


async def normalized_observation_version_matches_in_uow(
    session: AsyncSession, *, source_id: UUID, source_generation: int,
    document_id: UUID, document_version_id: UUID, external_id: str,
    provider: str, provider_scope_discriminator: str,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Prove one exact held normalized World version through a nonlocking owner read.

    Original admission/Source and Document/identity parents remain held; this bool
    grants no write authority and exposes no ORM/content. Require same workspace,
    Source/key, live canonical identity, generation, typed provider and current
    non-secret provider scope. No current-version condition is imposed because O
    chooses current only after this proof. Accepted journal/SourceObservation
    lineage and FK parent preparation remain Ingestion's mandatory responsibility.
    Missing/deleted/mismatching evidence returns False, stale fences abort; no I/O.
    """
    from modules.connectors import public as connectors

    if (not isinstance(external_id, str) or not 1 <= len(external_id) <= 512
            or provider not in MEASUREMENT_PROVIDER_IDS):
        return False
    projection = await _normalized_source_proof(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    current_scope = await connectors.get_current_provider_scope(
        session, source_id, source_generation, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (projection.provider != provider or current_scope is None
            or current_scope.workspace_id != scope.workspace_id or current_scope.source_id != source_id
            or current_scope.source_generation != source_generation or current_scope.provider_id != provider
            or current_scope.discriminator != provider_scope_discriminator):
        return False
    rows = (await session.execute(select(NormalizedVersionProvenance.provenance_json).select_from(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(NormalizedDocumentIdentity, and_(
            NormalizedDocumentIdentity.document_id == Document.id,
            NormalizedDocumentIdentity.source_id == Document.source_id,
            NormalizedDocumentIdentity.external_id == Document.external_id,
            NormalizedDocumentIdentity.workspace_id == Document.workspace_id,
        ))
        .join(NormalizedVersionProvenance, and_(
            NormalizedVersionProvenance.document_version_id == DocumentVersion.id,
            NormalizedVersionProvenance.document_id == Document.id,
        )).where(
            Document.id == document_id, Document.workspace_id == scope.workspace_id,
            Document.source_id == source_id, Document.external_id == external_id,
            DocumentVersion.id == document_version_id,
            NormalizedDocumentIdentity.tombstoned_at.is_(None),
            NormalizedVersionProvenance.provider_id == external_id,
            NormalizedVersionProvenance.source_generation == source_generation,
        ).limit(2))).all()
    if len(rows) != 1:
        return False
    provenance = rows[0][0]
    if not isinstance(provenance, dict) or provenance.get("provider_scope_discriminator") != provider_scope_discriminator:
        return False
    record = provenance.get("provider_record")
    if not isinstance(record, dict):
        return False
    try:
        metadata = ProviderRecordMetadata.model_validate(record)
    except (ValueError, TypeError):
        return False
    return metadata.provider == provider and metadata.world_data is not None


async def select_current_world_document_version_in_uow(
    session: AsyncSession, *, document_id: UUID, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
    preparation: NormalizedDocumentPreparation,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Select the O-approved exact World revision on an already-held prepared Document.

    Caller just selected this version through trusted O in this transaction.
    Compare original fences and latest key mapping; no Source/Document/version
    lock or acquiring helper is entered after outbox. Identity snapshot fields
    stay unchanged. Missing provenance returns False before mutation; no commit.
    """
    if (not isinstance(preparation, NormalizedDocumentPreparation) or preparation.workspace_id != scope.workspace_id
            or preparation.source_generation != expected_source_generation):
        raise RuntimeError("normalized_preparation_header_changed")
    projection = await _normalized_source_proof(
        session, preparation.source_id, expected_source_generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    matching = [key for key in preparation.keys if key.document_id == document_id and key.tombstoned_at is None]
    if len(matching) != 1:
        raise RuntimeError("normalized_preparation_document_missing")
    _, document, _ = await _normalized_target(session, matching[0].external_id, preparation)
    if document is None or not await normalized_observation_version_matches_in_uow(
        session, source_id=preparation.source_id, source_generation=expected_source_generation,
        document_id=document_id, document_version_id=document_version_id,
        external_id=matching[0].external_id, provider=projection.provider or "",
        provider_scope_discriminator=provider_scope_discriminator, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    ):
        return False
    return await _select_world_document_version(session, document, document_version_id,
                                                expected_source_generation, provider_scope_discriminator)


async def _select_world_document_version(
    session: AsyncSession, document: Document, document_version_id: UUID,
    expected_source_generation: int, provider_scope_discriminator: str,
) -> bool:
    """Mutate only an already-held Document's current/metadata projection, never lock.

    Exact version/provenance/source-key/generation/scope must match. The caller has
    freshly validated live identity/provider admission; missing evidence returns
    False before mutation. Projection flush is part of the caller's transaction.
    """
    selected = (await session.execute(
        select(DocumentVersion, NormalizedVersionProvenance)
        .join(NormalizedVersionProvenance, NormalizedVersionProvenance.document_version_id == DocumentVersion.id)
        .where(
            DocumentVersion.id == document_version_id, DocumentVersion.document_id == document.id,
            NormalizedVersionProvenance.document_id == document.id,
            NormalizedVersionProvenance.provider_id == document.external_id,
            NormalizedVersionProvenance.source_generation == expected_source_generation,
        ).execution_options(populate_existing=True)
    )).one_or_none()
    if selected is None:
        return False
    version, provenance = selected
    if provenance.provenance_json.get("provider_scope_discriminator") != provider_scope_discriminator:
        return False
    document.current_version = version.version_number
    document.content_hash = version.content_hash
    document.title = provenance.title
    document.canonical_url = provenance.canonical_url
    document.published_at = provenance.published_at
    document.content_type = provenance.content_type
    document.observed_at = provenance.observed_at
    document.language = _provenance_language(provenance.provenance_json)
    document.extraction_status = "ready"
    await session.flush()
    return True
