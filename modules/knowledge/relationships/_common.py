"""Shared admission, scope, row mapping and support-closure primitives for relationships.

Private implementation of ``modules.knowledge.relationships.public``; import from that facade.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import (
    ColumnElement,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.workspaces import public as workspaces
from core.workspaces.schemas import (
    AccessFence,
    AdminWorkspaceReadContext,
    InternalJobScope,
    Scope,
    WorkspaceContext,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
)
from modules.knowledge.relationships.schemas import (
    EvidenceRead,
    RelationshipCreate,
    RelationshipRead,
)
from modules.sources.public import SourceFence


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before relationship locks."""
    if not isinstance(scope, (WorkspaceContext, AdminWorkspaceReadContext, InternalJobScope)):
        raise TypeError("An explicit relationship workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock and isinstance(scope, AdminWorkspaceReadContext):
        raise HTTPException(status_code=403, detail="Admin workspace access is read-only")
    if lock:
        return await workspaces.lock_access_fence(
            session,
            scope=scope,
            expected=expected,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


MAX_CLEANUP_SUPPORTS = 10_000


def _evidence_scope(scope: Scope) -> ColumnElement[bool]:
    """Scope support rows through their workspace-owned relationship; the child has no scope column."""
    owned = aliased(
        Relationship
    )  # aliased: never auto-correlate with an enclosing Relationship query
    return RelationshipEvidence.relationship_id.in_(
        select(owned.id).where(owned.workspace_id == scope.workspace_id)
    )


def _support_pairs(refs: list[tuple[UUID, UUID]]) -> set[tuple[str, str]]:
    """Normalize ``(document_version_id, chunk_id)`` pairs to string tuples for set comparison."""
    return {(str(version), str(chunk)) for version, chunk in refs}


@dataclass(frozen=True)
class RelationshipSupportClosure:
    """Workspace-qualified ID closure of relationship support for one Source or Document."""

    source_id: UUID
    document_id: UUID | None
    membership_ids: tuple[UUID, ...]
    relationship_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    endpoint_entity_ids: tuple[UUID, ...]
    history_ids: tuple[int, ...]
    overflow: bool


def _require_cleanup_fences(
    actual: AccessFence,
    *,
    closure: RelationshipSupportClosure,
    scope: Scope,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Require the live access fence and Source to still match those held by a cleanup transaction.

    Raises HTTP 409 "Cleanup authority changed" (kept as ``HTTPException``: purge callers in sources/documents/
    ingestion branch on ``HTTPException`` status codes) when the fence differs or the Source id/workspace does not
    match the closure. Pure comparison; no I/O.
    """
    if (
        actual != access_fence
        or source_fence.id != closure.source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise HTTPException(status_code=409, detail="Cleanup authority changed")


async def _schedule_relationship_change(
    session: AsyncSession,
    relationship_id: UUID,
    fields: list[str],
    *,
    deleted: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Queue detached exact support in the same owner transaction before commit."""
    from modules.knowledge.temporal import public as temporal

    row = await session.scalar(
        select(Relationship).where(
            Relationship.workspace_id == scope.workspace_id,
            Relationship.id == relationship_id,
        )
    )
    if row is None:
        return
    support = list(
        (
            await session.execute(
                select(
                    RelationshipEvidence.document_version_id,
                    RelationshipEvidence.chunk_id,
                )
                .where(
                    _evidence_scope(scope), RelationshipEvidence.relationship_id == relationship_id
                )
                .limit(MAX_CLEANUP_SUPPORTS + 1)
            )
        ).all()
    )
    if len(support) > MAX_CLEANUP_SUPPORTS:
        raise ValueError("Relationship change exceeds complete support bound")
    await temporal.schedule_canonical_change(
        session,
        kind="relationship",
        canonical_id=relationship_id,
        revision=None,
        fields=fields,
        support=[(version, chunk) for version, chunk in support],
        origin=row.origin,
        deleted=deleted,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


def _relationship_read(
    relationship: Relationship, evidence: list[EvidenceRead] | None = None
) -> RelationshipRead:
    """Project a stored relationship and optional evidence into its public DTO."""
    return RelationshipRead(
        id=relationship.id,
        source_entity_id=relationship.source_entity_id,
        target_entity_id=relationship.target_entity_id,
        type=relationship.type,
        origin=relationship.origin,
        confidence=relationship.confidence,
        valid_from=relationship.valid_from,
        valid_to=relationship.valid_to,
        metadata=relationship.metadata_json,
        created_at=relationship.created_at,
        evidence=evidence or [],
        validity_precision="bounded"
        if relationship.valid_from is not None and relationship.valid_to is not None
        else "unknown",
    )


async def _evidence_read(
    session: AsyncSession,
    rows: list[RelationshipEvidence],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[EvidenceRead]:
    """Join support rows to document-owned provenance and excerpts."""
    if not rows:
        return []
    pairs = list(dict.fromkeys((row.document_version_id, row.chunk_id) for row in rows))
    refs = await documents.read_evidence_refs(
        session, pairs, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    by_pair = {(ref.document_version_id, ref.chunk_id): ref for ref in refs}
    return [
        EvidenceRead(
            id=row.id,
            relationship_id=row.relationship_id,
            document_id=ref.document_id,
            document_version_id=ref.document_version_id,
            version_number=ref.version_number,
            chunk_id=ref.chunk_id,
            observed_at=ref.observed_at,
            extracted_at=row.extracted_at,
            confidence=row.confidence,
            source_entity_membership_id=row.source_membership_id,
            target_entity_membership_id=row.target_membership_id,
            title=ref.title,
            canonical_url=ref.canonical_url,
            source_id=ref.source_id,
            excerpt=ref.excerpt,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
        )
        for row in rows
        if (ref := by_pair.get((row.document_version_id, row.chunk_id))) is not None
    ]


def _build_evidence_rows(
    payload: RelationshipCreate,
    by_pair: dict[tuple[UUID, UUID], Any],
    memberships_by_id: dict[UUID, Any],
) -> list[RelationshipEvidence]:
    """Validate each evidence ref and endpoint membership and build the (unattached) evidence rows (pure).

    Raises ``ValueError`` when evidence does not exist, a derived relationship omits an endpoint membership,
    or a membership does not belong to the expected endpoint entity and the same version/chunk pair.
    """
    evidence_rows: list[RelationshipEvidence] = []
    for item in payload.evidence:
        pair = (item.document_version_id, item.chunk_id)
        ref = by_pair.get(pair)
        if ref is None:
            raise ValueError("Relationship evidence does not exist")
        for membership_id, expected_entity_id in (
            (item.source_membership_id, payload.source_entity_id),
            (item.target_membership_id, payload.target_entity_id),
        ):
            membership = memberships_by_id.get(membership_id) if membership_id else None
            if payload.origin == "derived" and membership is None:
                raise ValueError("Derived evidence must identify both endpoint memberships")
            if membership is not None and (
                membership.entity_id != expected_entity_id
                or (membership.document_version_id, membership.chunk_id) != pair
            ):
                raise ValueError(
                    "Evidence membership does not match the relationship endpoint and evidence"
                )
        evidence_rows.append(
            RelationshipEvidence(
                document_version_id=item.document_version_id,
                chunk_id=item.chunk_id,
                document_id=ref.document_id,
                source_id=ref.source_id,
                observed_at=ref.observed_at,
                confidence=item.confidence,
                source_membership_id=item.source_membership_id,
                target_membership_id=item.target_membership_id,
            )
        )
    return evidence_rows
