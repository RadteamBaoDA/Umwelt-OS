"""Document correction-decision reads.

Private implementation of ``modules.knowledge.entities.public``; import from that facade.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.entities._common import _admit
from modules.knowledge.entities.models import (
    EntityCorrectionDecision,
    EntityEvidenceMembership,
)


async def _read_decision_rows(
    session: AsyncSession, *, scope: Scope, document_id: UUID, document_version_id: UUID,
    fingerprints: set[str], chunk_ids: set[UUID], for_update: bool,
) -> tuple[
    list[EntityEvidenceMembership], list[EntityCorrectionDecision],
    list[tuple[EntityCorrectionDecision, UUID, str | None]], list[EntityCorrectionDecision],
]:
    """Read matching memberships, evidence-scoped decisions and document-scoped decisions (each bounded to 201).

    Query order is memberships -> evidence decisions (skipped when no memberships) -> document decisions;
    with ``for_update`` each query takes ``FOR UPDATE`` in that order. Returns the memberships, the evidence
    decisions, the evidence decisions paired with their membership chunk/fingerprint, and the document rows.
    """
    membership_query = select(EntityEvidenceMembership).where(
        EntityEvidenceMembership.workspace_id == scope.workspace_id,
        EntityEvidenceMembership.document_id == document_id,
        EntityEvidenceMembership.document_version_id == document_version_id,
        EntityEvidenceMembership.match_fingerprint.in_(fingerprints),
        EntityEvidenceMembership.chunk_id.in_(chunk_ids),
    ).order_by(EntityEvidenceMembership.entity_id, EntityEvidenceMembership.id).limit(201)
    if for_update:
        membership_query = membership_query.with_for_update()
    memberships = list((await session.scalars(
        membership_query.execution_options(populate_existing=True)
    )).all())
    membership_ids = sorted({row.id for row in memberships}, key=str)
    decisions_query = select(EntityCorrectionDecision).where(
        EntityCorrectionDecision.workspace_id == scope.workspace_id,
        EntityCorrectionDecision.scope == "evidence",
        EntityCorrectionDecision.document_id.is_(None),
        EntityCorrectionDecision.membership_id.in_(membership_ids),
    ).order_by(EntityCorrectionDecision.id).limit(201)
    if for_update:
        decisions_query = decisions_query.with_for_update()
    evidence_decisions = list((await session.scalars(
        decisions_query.execution_options(populate_existing=True)
    )).all()) if membership_ids else []
    membership_by_id = {row.id: row for row in memberships}
    evidence_rows = [
        (row, membership_by_id[row.membership_id].chunk_id,
         membership_by_id[row.membership_id].match_fingerprint)
        for row in evidence_decisions if row.membership_id in membership_by_id
    ]
    document_query = select(EntityCorrectionDecision).where(
            EntityCorrectionDecision.workspace_id == scope.workspace_id,
            EntityCorrectionDecision.scope == "document",
            EntityCorrectionDecision.document_id == document_id,
            EntityCorrectionDecision.match_fingerprint.in_(fingerprints),
        ).order_by(EntityCorrectionDecision.id).limit(201)
    if for_update:
        document_query = document_query.with_for_update()
    document_rows = list((await session.scalars(
        document_query.execution_options(populate_existing=True)
    )).all())
    return memberships, evidence_decisions, evidence_rows, document_rows


def _resolve_correction_decisions(
    candidates: dict[str, tuple[str, set[UUID]]], memberships: list[EntityEvidenceMembership],
    evidence_decisions: list[EntityCorrectionDecision],
    evidence_rows: list[tuple[EntityCorrectionDecision, UUID, str | None]],
    document_rows: list[EntityCorrectionDecision],
) -> dict[str, tuple[UUID, str, UUID | None] | None]:
    """Fold the read rows into one decision per candidate (pure).

    A candidate resolves to its newest matching decision, ``None`` when there is none, or a ``conflict``
    (nil-UUID id when nothing matched) when rows hit a read bound, evidence is partial or fingerprint-ambiguous,
    selectors overlap between candidates, or evidence and document decisions disagree.
    """
    memberships_by_selector: dict[tuple[str, UUID], list[EntityEvidenceMembership]] = {}
    for membership in memberships:
        if membership.match_fingerprint is not None:
            memberships_by_selector.setdefault(
                (membership.match_fingerprint, membership.chunk_id), []
            ).append(membership)
    by_document_fingerprint: dict[str, list[EntityCorrectionDecision]] = {}
    for row in document_rows:
        if row.match_fingerprint is not None:
            by_document_fingerprint.setdefault(row.match_fingerprint, []).append(row)
    current_candidates_by_selector: dict[tuple[str, UUID], list[str]] = {}
    current_candidates_by_fingerprint: dict[str, list[str]] = {}
    for request_key, (fingerprint, chunks) in candidates.items():
        current_candidates_by_fingerprint.setdefault(fingerprint, []).append(request_key)
        for chunk_id in chunks:
            current_candidates_by_selector.setdefault((fingerprint, chunk_id), []).append(request_key)
    by_candidate: dict[str, list[tuple[EntityCorrectionDecision, UUID, bool]]] = {}
    for row, chunk_id, membership_fingerprint in evidence_rows:
        for request_key, (fingerprint, chunks) in candidates.items():
            if membership_fingerprint == fingerprint and chunk_id in chunks:
                by_candidate.setdefault(request_key, []).append((
                    row, chunk_id, row.match_fingerprint == fingerprint,
                ))
    result: dict[str, tuple[UUID, str, UUID | None] | None] = {}
    for request_key, (fingerprint, chunks) in candidates.items():
        all_evidence = by_candidate.get(request_key, [])
        evidence_matches = [
            (row, chunk) for row, chunk, exact_fingerprint in all_evidence
            if exact_fingerprint
        ]
        document_matches = by_document_fingerprint.get(fingerprint, [])
        evidence_states = {(row.decision, row.entity_id) for row, _ in evidence_matches}
        document_states = {(row.decision, row.entity_id) for row in document_matches}
        partial = bool(all_evidence) and {chunk for _, chunk in evidence_matches} != chunks
        fingerprint_ambiguous = any(not exact_fingerprint for _, _, exact_fingerprint in all_evidence)
        membership_ambiguous = any(
            len(memberships_by_selector.get((fingerprint, chunk_id), [])) > 1
            for chunk_id in chunks
        )
        current_evidence_ambiguous = any(
            len(current_candidates_by_selector.get((fingerprint, chunk_id), [])) > 1
            for chunk_id in chunks
        )
        current_document_ambiguous = (
            bool(document_matches)
            and len(current_candidates_by_fingerprint.get(fingerprint, [])) > 1
        )
        conflicting = (
            len(memberships) > 200 or len(evidence_decisions) > 200
            or len(document_rows) > 200 or partial or fingerprint_ambiguous
            or membership_ambiguous or current_evidence_ambiguous
            or current_document_ambiguous or len(evidence_states) > 1
            or len(document_states) > 1
        )
        conflicting = conflicting or bool(evidence_states and document_states and evidence_states != document_states)
        all_matches = [row for row, _, _ in all_evidence] + document_matches
        if conflicting:
            latest = max(all_matches, key=lambda row: (row.created_at, row.id)) if all_matches else None
            result[request_key] = (latest.id if latest else UUID(int=0), "conflict", None)
        elif not all_matches:
            result[request_key] = None
        else:
            latest = max(all_matches, key=lambda row: (row.created_at, row.id))
            result[request_key] = (latest.id, latest.decision, latest.entity_id)
    return result


async def get_document_correction_decisions(
    session: AsyncSession, document_id: UUID, document_version_id: UUID,
    candidates: dict[str, tuple[str, set[UUID]]],
    *, scope: Scope, multi_workspace_enabled: bool, for_update: bool = False,
) -> dict[str, tuple[UUID, str, UUID | None] | None]:
    """Resolve bounded evidence/document owner decisions, reporting ambiguous conflicts.

    Admits the scope (read fence), enforces at most 30 candidates and 200 distinct chunks (``ValueError``),
    then reads rows (``FOR UPDATE`` when requested) and resolves each candidate; see
    ``_resolve_correction_decisions`` for the conflict rules.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(candidates) > 30:
        raise ValueError("Correction decision lookup exceeds its candidate limit")
    if not candidates:
        return {}
    fingerprints = {spec[0] for spec in candidates.values()}
    chunk_ids = {chunk_id for _, chunks in candidates.values() for chunk_id in chunks}
    if len(chunk_ids) > 200 or any(not chunks for _, chunks in candidates.values()):
        raise ValueError("Correction evidence binding exceeds its chunk limit")
    memberships, evidence_decisions, evidence_rows, document_rows = await _read_decision_rows(
        session, scope=scope, document_id=document_id, document_version_id=document_version_id,
        fingerprints=fingerprints, chunk_ids=chunk_ids, for_update=for_update,
    )
    return _resolve_correction_decisions(candidates, memberships, evidence_decisions, evidence_rows, document_rows)
