"""Shared limits, admission, conflict error and closure dataclasses for entity corrections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.admission import admit_scope
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityFieldEvidence,
    EntityRedirect,
)
from modules.knowledge.entities.schemas import (
    EntityCorrectionConflict,
)
from modules.knowledge.relationships import public as relationships
from modules.knowledge.relationships.public import CorrectionRelationshipRef

MAX_CORRECTION_ENTITIES = 100
MAX_CORRECTION_MEMBERSHIPS = 200
MAX_CORRECTION_EVIDENCE_REFS = 100


async def _admit(
    session: AsyncSession,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    lock: bool = False,
    expected: AccessFence | None = None,
) -> AccessFence:
    """Admit the owner workspace before correction discovery and lock acquisition.

    Delegates to the canonical ``admit_scope`` for workspace/job scopes only (owner-only members get 403, the
    flag must be a ``bool``); ``lock=True`` locks the access fence against ``expected``. No commit.
    """
    return await admit_scope(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        lock=lock,
        expected=expected,
        allowed_scopes=(WorkspaceContext, InternalJobScope),
        scope_label="correction workspace",
    )


class CorrectionConflictError(ValueError):
    """Carry an API-ready correction conflict alongside its validation error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        entity_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
        membership_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
        relationship_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    ) -> None:
        """Build a conflict DTO containing the involved entity and support IDs."""
        super().__init__(message)
        self.conflict = EntityCorrectionConflict(
            code=code,
            message=message,
            entity_ids=entity_ids,
            membership_ids=membership_ids,
            relationship_ids=relationship_ids,
        )


@dataclass
class _Closure:
    """Hold the bounded entity, evidence, and relationship snapshot for correction."""

    entity_rows: list[Entity]
    memberships: list[EntityEvidenceMembership]
    aliases: list[EntityAlias]
    alias_supports: list[EntityAliasEvidence]
    field_supports: list[EntityFieldEvidence]
    relationship_refs: list[CorrectionRelationshipRef]
    redirect_rows: list[EntityRedirect]
    relationship_entity_ids: list[UUID]
    entity_ids: list[UUID]
    evidence_pairs: list[tuple[UUID, UUID]]
    source_ids: list[UUID]
    document_ids: list[UUID]
    membership_ids: list[UUID]
    timeline_event_ids: list[UUID]

    @property
    def relationship_ids(self) -> list[UUID]:
        """Return relationship IDs in the captured correction closure."""
        return [item.id for item in self.relationship_refs]

    def signature(self) -> str:
        """Serialize every relevant row field for optimistic closure revalidation."""
        value = {
            "entities": [
                (
                    str(row.id),
                    row.revision,
                    row.type,
                    row.name,
                    row.canonical_name,
                    row.description,
                    row.name_origin,
                    row.description_origin,
                    row.metadata_json,
                )
                for row in self.entity_rows
            ],
            "memberships": [
                (
                    str(row.id),
                    str(row.entity_id),
                    str(row.document_id),
                    str(row.source_id),
                    str(row.document_version_id),
                    str(row.chunk_id),
                    row.extraction_identity,
                    row.candidate_key,
                    row.match_fingerprint,
                    row.observed_at.isoformat(),
                    row.extracted_at.isoformat(),
                    row.confidence,
                )
                for row in self.memberships
            ],
            "aliases": [
                (
                    str(row.id),
                    str(row.entity_id),
                    row.normalized_alias,
                    row.alias,
                    str(row.source_id),
                    row.confirmed,
                    row.origin,
                )
                for row in self.aliases
            ],
            "alias_supports": [
                (str(row.id), str(row.alias_id), str(row.membership_id), row.confidence)
                for row in self.alias_supports
            ],
            "field_supports": [
                (
                    str(row.id),
                    str(row.entity_id),
                    row.field_name,
                    row.value_hash,
                    str(row.membership_id),
                )
                for row in self.field_supports
            ],
            "relationships": [item.model_dump(mode="json") for item in self.relationship_refs],
            "redirects": [
                (str(row.old_entity_id), str(row.target_entity_id), row.actor_id, row.reason)
                for row in self.redirect_rows
            ],
            "relationship_entities": [str(item) for item in self.relationship_entity_ids],
            "entity_ids": [str(item) for item in self.entity_ids],
            "evidence_pairs": [(str(left), str(right)) for left, right in self.evidence_pairs],
            "source_ids": [str(item) for item in self.source_ids],
            "document_ids": [str(item) for item in self.document_ids],
            "membership_ids": [str(item) for item in self.membership_ids],
            "timeline_events": [str(item) for item in self.timeline_event_ids],
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class _DeleteClosure:
    """Hold the bounded entity and graph rows discovered for canonical deletion."""

    entity_rows: list[Entity]
    redirect_rows: list[EntityRedirect]
    memberships: list[EntityEvidenceMembership]
    aliases: list[EntityAlias]
    alias_supports: list[EntityAliasEvidence]
    field_supports: list[EntityFieldEvidence]
    decisions: list[EntityCorrectionDecision]
    relationship_refs: list[CorrectionRelationshipRef]
    evidence_pairs: list[tuple[UUID, UUID]]
    source_ids: list[UUID]
    document_ids: list[UUID]
    lock_entity_ids: list[UUID]
    timeline_event_ids: list[UUID]

    @property
    def entity_ids(self) -> list[UUID]:
        """Return the sorted IDs of canonical and redirected entities in the closure."""
        return sorted((item.id for item in self.entity_rows), key=str)

    @property
    def relationship_ids(self) -> list[UUID]:
        """Return sorted relationship IDs captured for deletion."""
        return sorted((item.id for item in self.relationship_refs), key=str)

    def signature(self) -> str:
        """Serialize ownership, provenance, support, and graph state for revalidation."""
        return json.dumps(
            {
                "entities": [
                    (
                        str(item.id),
                        item.revision,
                        item.name,
                        item.canonical_name,
                        item.description,
                        item.name_origin,
                        item.description_origin,
                        item.metadata_json,
                    )
                    for item in self.entity_rows
                ],
                "redirects": [
                    (
                        str(item.old_entity_id),
                        str(item.target_entity_id),
                        str(item.actor_id),
                        item.reason,
                    )
                    for item in self.redirect_rows
                ],
                "memberships": [
                    (
                        str(item.id),
                        str(item.entity_id),
                        str(item.document_id),
                        str(item.source_id),
                        str(item.document_version_id),
                        str(item.chunk_id),
                        item.extraction_identity,
                        item.candidate_key,
                        item.match_fingerprint,
                        item.observed_at.isoformat(),
                        item.extracted_at.isoformat(),
                        item.confidence,
                    )
                    for item in self.memberships
                ],
                "aliases": [
                    (
                        str(item.id),
                        str(item.entity_id),
                        str(item.source_id),
                        item.alias,
                        item.normalized_alias,
                        item.confirmed,
                        item.origin,
                        item.confidence,
                    )
                    for item in self.aliases
                ],
                "alias_supports": [
                    (str(item.id), str(item.alias_id), str(item.membership_id), item.confidence)
                    for item in self.alias_supports
                ],
                "field_supports": [
                    (
                        str(item.id),
                        str(item.entity_id),
                        item.field_name,
                        item.value_hash,
                        str(item.membership_id),
                    )
                    for item in self.field_supports
                ],
                "decisions": [
                    (
                        str(item.id),
                        item.decision,
                        item.scope,
                        str(item.entity_id),
                        str(item.document_id),
                        str(item.membership_id),
                        item.match_fingerprint,
                        item.actor_id,
                        item.reason,
                        item.created_at.isoformat(),
                    )
                    for item in self.decisions
                ],
                "relationships": [item.model_dump(mode="json") for item in self.relationship_refs],
                "evidence": [(str(left), str(right)) for left, right in self.evidence_pairs],
                "sources": [str(item) for item in self.source_ids],
                "documents": [str(item) for item in self.document_ids],
                "lock_entities": [str(item) for item in self.lock_entity_ids],
                "timeline_events": [str(item) for item in self.timeline_event_ids],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def _conflict(
    code: str,
    message: str,
    *,
    entity_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    membership_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
    relationship_ids: list[UUID] = [],  # noqa: B006  # never mutated; FastAPI/DTO copies the default
) -> CorrectionConflictError:
    """Build a structured correction conflict with the supplied affected IDs."""
    return CorrectionConflictError(
        code,
        message,
        entity_ids=entity_ids,
        membership_ids=membership_ids,
        relationship_ids=relationship_ids,
    )


async def _temporal_before_relationships(
    session: AsyncSession,
    relationship_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Record actual pre-correction owner state before endpoint memberships move; history starts at this observation."""
    for relationship_id in relationship_ids:
        await relationships.record_relationship_history(
            session, relationship_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )


async def _temporal_correction(
    session: AsyncSession,
    memberships: list[EntityEvidenceMembership],
    entity_revisions: list[tuple[UUID, int]],
    operation: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    deleted: bool = False,
) -> None:
    """Flush identifier-only desired-state changes with the canonical correction's existing atomic commit."""
    from modules.knowledge.temporal import public as temporal

    pairs = sorted(
        {(member.document_version_id, member.chunk_id) for member in memberships}, key=str
    )
    for entity_id, revision in entity_revisions:
        await temporal.schedule_canonical_change(
            session,
            kind="entity",
            canonical_id=entity_id,
            revision=revision,
            fields=[operation],
            support=pairs,
            origin="owner",
            deleted=deleted,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
