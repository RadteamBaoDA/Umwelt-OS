"""Normalized document identity: source proof, key states, preparation and raw-URI identity lock."""

from typing import cast
from uuid import UUID

from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents._scope import _admit_document_scope, _document_scope
from modules.knowledge.documents.models import (
    Document,
    NormalizedDocumentIdentity,
)
from modules.knowledge.documents.schemas import (
    NormalizedDocumentKeyState,
    NormalizedDocumentPreparation,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence


async def _normalized_source_proof(
    session: AsyncSession, source_id: UUID, source_generation: int, *,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> ConnectorSource:
    """Freshly compare complete original access/Source fences without taking locks.

    Owner public reads enforce real owner/default-workspace admission and a bound
    internal Source generation. Callers retain earlier admission/Source locks;
    any stale, inactive or foreign fence aborts the whole transaction, with no
    membership/epoch rebasing, mutation, commit or remote I/O.
    """
    if not isinstance(access_fence, AccessFence) or not isinstance(source_fence, SourceFence):
        raise RuntimeError("normalized_preparation_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    current_access = await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    current_source = await sources.get_source_fence(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (current_access != access_fence or current_source != source_fence
            or source_fence.id != source_id or source_fence.workspace_id != scope.workspace_id
            or access_fence.workspace_id != scope.workspace_id
            or source_fence.status != "active" or source_fence.generation != source_generation):
        raise RuntimeError("normalized_preparation_fence_changed")
    projection = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (projection is None or projection.workspace_id != scope.workspace_id
            or projection.id != source_id or projection.status != "active"
            or projection.generation != source_generation):
        raise RuntimeError("normalized_preparation_source_changed")
    return projection


async def _normalized_key_states(
    session: AsyncSession, source_id: UUID, external_ids: tuple[str, ...], workspace_id: UUID,
) -> tuple[tuple[NormalizedDocumentKeyState, ...], dict[str, Document], dict[str, NormalizedDocumentIdentity]]:
    """Read at most32 exact namespaces freshly, without following foreign pointers.

    The Source is already admitted/held. Namespace corruption and a live identity
    whose canonical Document vanished are transaction conflicts. A normal
    non-normalized key collision is retained as comparison data for early input
    rejection. No locks or writes occur and no ORM leaves the Documents owner.
    """
    documents = {cast(str, row.external_id): row for row in (await session.scalars(select(Document).where(
        Document.source_id == source_id, Document.external_id.in_(external_ids),
    ).execution_options(populate_existing=True))).all()}
    identities = {row.external_id: row for row in (await session.scalars(select(NormalizedDocumentIdentity).where(
        NormalizedDocumentIdentity.source_id == source_id,
        NormalizedDocumentIdentity.external_id.in_(external_ids),
    ).execution_options(populate_existing=True))).all()}
    states = []
    for external_id in external_ids:
        document, identity = documents.get(external_id), identities.get(external_id)
        if any(row is not None and (row.workspace_id != workspace_id or row.source_id != source_id
                                    or row.external_id != external_id) for row in (document, identity)):
            raise RuntimeError("normalized_preparation_namespace_changed")
        if identity is not None:
            if identity.document_id is not None and (document is None or identity.document_id != document.id):
                raise RuntimeError("normalized_preparation_foreign_pointer")
            if identity.tombstoned_at is None and document is None:
                raise RuntimeError("normalized_preparation_canonical_missing")
        try:
            states.append(NormalizedDocumentKeyState(
                external_id=external_id, document_id=document.id if document is not None else None,
                normalized_identity_id=identity.id if identity is not None else None,
                identity_document_id=identity.document_id if identity is not None else None,
                tombstoned_at=identity.tombstoned_at if identity is not None else None,
            ))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("normalized_preparation_stored_shape_changed") from exc
    return tuple(states), documents, identities


async def prepare_normalized_document_keys(
    session: AsyncSession, source_id: UUID, external_ids: tuple[str, ...], *,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> NormalizedDocumentPreparation:
    """Prepare <=32 keys after Source and before Ingestion/Observation/outbox roots.

    Reject oversized input before querying, preserve spelling and deduplicate
    discovery keys only. Lock all discovered Documents by UUID, then all identity
    rows by UUID; compare freshly loaded mappings/absence/tombstones against
    discovery. A changed set aborts rather than acquiring another earlier root.
    Empty input is valid. No placeholders, mutation, commit or lock token exists.
    """
    if (not isinstance(source_id, UUID) or not isinstance(external_ids, tuple)
            or len(external_ids) > 32 or any(type(key) is not str or not 1 <= len(key) <= 512 for key in external_ids)):
        raise ValueError("Normalized preparation requires at most32 exact external keys")
    external_ids = tuple(dict.fromkeys(external_ids))
    if not isinstance(source_fence, SourceFence):
        raise RuntimeError("normalized_preparation_fence_required")  # noqa: TRY004 - fence contract raises RuntimeError by design
    await _normalized_source_proof(
        session, source_id, source_fence.generation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    before, documents, identities = await _normalized_key_states(session, source_id, external_ids, scope.workspace_id)
    if documents:
        await session.scalars(select(Document).where(Document.id.in_([row.id for row in documents.values()]))
                              .order_by(Document.id).with_for_update().execution_options(populate_existing=True))
    if identities:
        await session.scalars(select(NormalizedDocumentIdentity).where(
            NormalizedDocumentIdentity.id.in_([row.id for row in identities.values()]),
        ).order_by(NormalizedDocumentIdentity.id).with_for_update().execution_options(populate_existing=True))
    after, _, _ = await _normalized_key_states(session, source_id, external_ids, scope.workspace_id)
    if before != after:
        raise RuntimeError("normalized_preparation_identity_changed")
    return NormalizedDocumentPreparation(workspace_id=scope.workspace_id, source_id=source_id,
                                         source_generation=source_fence.generation, keys=after)


async def _normalized_target(
    session: AsyncSession, external_id: str, preparation: NormalizedDocumentPreparation,
) -> tuple[NormalizedDocumentKeyState, Document | None, NormalizedDocumentIdentity | None]:
    """Compare exactly one prepared key freshly; never adopt an unexpected incumbent."""
    expected = next((key for key in preparation.keys if key.external_id == external_id), None)
    if expected is None:
        raise RuntimeError("normalized_preparation_key_missing")
    fresh, documents, identities = await _normalized_key_states(
        session, preparation.source_id, (external_id,), preparation.workspace_id,
    )
    if fresh != (expected,):
        raise RuntimeError("normalized_preparation_identity_changed")
    return expected, documents.get(external_id), identities.get(external_id)


class NormalizedDocumentValidationRejected(ValueError):
    """Reject one normalized record before any of that record's owner effects.

    Only explicit input/provider/order/collision validation emits this exception.
    Held identity/fence/SQL conflicts and all post-mutation failures remain transaction
    failures. ValueError compatibility preserves the ordinary acquiring API contract.
    """


async def find_document_identity(
    session: AsyncSession, source_id: UUID, external_id: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> UUID | None:
    """Return the visible document ID a source owns for an external identity, if any.

    Mirrors ``has_document_identity``: admission and the workspace/purge predicate precede the
    query, so a foreign or data-purged key is None. Takes no lock and does not commit.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return await session.scalar(
        select(Document.id).where(
            Document.source_id == source_id, Document.external_id == external_id, *_document_scope(scope),
        ).limit(1)
    )


async def has_document_identity(
    session: AsyncSession, source_id: UUID, external_id: str, *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Check one exact visible canonical key for upload dedup, exposing no metadata.

    Actual owner/internal admission and Source lineage precede the query. A deleted,
    foreign or data-purged key is False, never an adopted tombstone or authority upgrade.
    Upload caller retains the Source lock; this method acquires no lock or commit.
    """
    await _admit_document_scope(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return bool(
        await session.scalar(
            select(Document.id).where(Document.source_id == source_id, Document.external_id == external_id,
                *_document_scope(scope)).limit(1)
        )
    )


async def lock_raw_uri_identity(session: AsyncSession, raw_uri: str) -> None:
    """Serialize raw-URI publication with its final ownership check and unlink transaction."""
    identity = f"documents.raw:{raw_uri}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )
