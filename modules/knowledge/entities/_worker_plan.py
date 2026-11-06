"""Candidate resolution planning: known candidates, owner decisions and review items for entity extraction."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.ids import uuid7
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.knowledge.entities._worker_claim import (
    _candidate_key,
    _Claim,
    _KnownCandidates,
    _OwnerDecision,
    _Plan,
    _Snapshot,
    _with_digest,
)
from modules.knowledge.entities.extraction import (
    ExtractedEntity,
    ExtractionOutput,
)
from modules.knowledge.entities.resolution import candidate_match_fingerprint, resolve_candidate
from modules.knowledge.entities.schemas import canonicalize_name

logger = logging.getLogger("modules.knowledge.entities.worker")  # same logger object as the facade


async def _read_evidence_by_chunk(
    session: AsyncSession,
    snap: _Snapshot,
    extracted: ExtractionOutput,
) -> dict[UUID, Any]:
    """Read evidence refs for every chunk cited by an extracted entity, keyed by chunk id.

    Raises ``ValueError("document_unavailable")`` when the documents owner reports the evidence is gone.
    """
    claim, data = snap.claim, snap.data
    membership_chunk_ids = sorted(
        {chunk_id for item in extracted.entities for chunk_id in item.chunk_ids}, key=str
    )
    evidence_refs = (
        await documents.read_extraction_evidence_refs(
            session,
            document_id=data.document_id,
            document_version_id=claim.version_id,
            source_id=data.source_id,
            source_generation=claim.captured_generation,
            chunk_ids=membership_chunk_ids,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
        if membership_chunk_ids
        else []
    )
    if evidence_refs is None:
        raise ValueError("document_unavailable")
    return {ref.chunk_id: ref for ref in evidence_refs}


async def _list_known_candidates(
    session: AsyncSession,
    claim: _Claim,
    names_by_type: dict[str, list[str]],
) -> _KnownCandidates:
    """List bounded resolution candidates per entity type (types iterate in first-seen order)."""
    known: _KnownCandidates = {}
    for entity_type, names in names_by_type.items():
        known[entity_type] = await entities.list_resolution_candidates(
            session,
            entity_type,
            names,
            scope=claim.scope,
            multi_workspace_enabled=claim.multi_workspace_enabled,
        )
    return known


def _initial_plan(
    candidates: list[ExtractedEntity], known_cache: _KnownCandidates
) -> dict[str, _Plan]:
    """Resolve every candidate against its type's known entities; bounded contexts become review-only."""
    plan: dict[str, _Plan] = {}
    for candidate in candidates:
        known, overflow = known_cache[candidate.type]
        if overflow:
            plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
            continue
        resolution, match_id, possible = resolve_candidate(candidate.name, candidate.type, known)
        plan[candidate.key] = (
            resolution,
            match_id,
            possible,
            "ambiguous_identity" if resolution == "review" else None,
        )
    return plan


def _pre_resolve_near_duplicates(candidates: list[ExtractedEntity], plan: dict[str, _Plan]) -> None:
    """Pre-resolve intra-response near-duplicates (in place) so resolve-then-lock cannot create two new rows.

    Same-type candidates that are both ``new``/``matched`` and whose canonical names are >= 0.78 similar
    become review-only; the ``new`` side of a new/matched pair cites the matched entity as a possible match.
    Pure: no I/O, iteration order is the response order.
    """
    for index, left in enumerate(candidates):
        left_resolution, left_id, _left_possible, _left_reason = plan[left.key]
        for right in candidates[index + 1 :]:
            if left.type != right.type:
                continue
            right_resolution, right_id, _right_possible, _right_reason = plan[right.key]
            if left_resolution not in {"new", "matched"} or right_resolution not in {
                "new",
                "matched",
            }:
                continue
            similarity = SequenceMatcher(
                None, canonicalize_name(left.name), canonicalize_name(right.name)
            ).ratio()
            if similarity < 0.78:
                continue
            if left_resolution == "new" and right_resolution == "new":
                plan[left.key] = ("review", None, [], "ambiguous_identity")
                plan[right.key] = ("review", None, [], "ambiguous_identity")
            elif left_resolution == "new" and right_resolution == "matched":
                plan[left.key] = (
                    "review",
                    None,
                    [right_id] if right_id else [],
                    "ambiguous_identity",
                )
            elif left_resolution == "matched" and right_resolution == "new":
                plan[right.key] = (
                    "review",
                    None,
                    [left_id] if left_id else [],
                    "ambiguous_identity",
                )


async def _apply_owner_decisions(
    session: AsyncSession,
    claim: _Claim,
    candidates: list[ExtractedEntity],
    plan: dict[str, _Plan],
    initial_decisions: dict[str, tuple[UUID, str, UUID | None] | None],
) -> dict[str, _OwnerDecision | None]:
    """Canonicalize owner correction decisions and let them override ``plan`` (in place).

    A target that no longer resolves becomes a ``conflict``. ``suppress`` and ``conflict`` force review;
    ``assign`` forces a match on the canonical target. Returns the canonical decision per candidate key so the
    refresh pass can detect later changes.
    """
    canonical_decisions: dict[str, _OwnerDecision | None] = {}
    for candidate in candidates:
        decision = initial_decisions.get(candidate.key)
        if decision is None:
            canonical_decisions[candidate.key] = None
            continue
        decision_id, action, target_id = decision
        if target_id is not None:
            try:
                target_id = await entities.resolve_canonical_entity_id(
                    session,
                    target_id,
                    scope=claim.scope,
                    multi_workspace_enabled=claim.multi_workspace_enabled,
                )
            except (LookupError, ValueError):
                action, target_id = "conflict", None
        canonical_decisions[candidate.key] = (decision_id, action, target_id)
        if action == "suppress":
            plan[candidate.key] = ("review", None, [], "owner_suppressed_candidate")
        elif action == "assign" and target_id is not None:
            plan[candidate.key] = ("matched", str(target_id), [], None)
        elif action == "conflict":
            plan[candidate.key] = ("review", None, [], "conflicting_owner_corrections")
    return canonical_decisions


async def _lock_matched_entities(
    session: AsyncSession, claim: _Claim, plan: dict[str, _Plan]
) -> dict[UUID, Any]:
    """Lock every matched entity FOR UPDATE in one call, in ``str``-sorted id order (deadlock-free).

    If an entity vanished or changed identity, every matched plan entry degrades (in place) to review with
    ``identity_changed_during_resolution``. Returns the locked refs keyed by requested id.
    """
    existing_ids = sorted(
        {
            UUID(match_id)
            for resolution, match_id, _, _ in plan.values()
            if resolution == "matched" and match_id is not None
        },
        key=str,
    )
    locked_refs: dict[UUID, Any] = {}
    if existing_ids:
        try:
            refs = await entities.get_entity_refs(
                session,
                existing_ids,
                for_write=True,
                scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
            locked_refs = {ref.requested_id: ref for ref in refs}
        except (LookupError, ValueError):
            for key, (resolution, match_id, possible, _reason) in list(plan.items()):
                if resolution == "matched":
                    plan[key] = (
                        "review",
                        None,
                        possible + ([match_id] if match_id else []),
                        "identity_changed_during_resolution",
                    )
    return locked_refs


async def _current_owner_decision(
    session: AsyncSession,
    claim: _Claim,
    decision: tuple[UUID, str, UUID | None] | None,
) -> _OwnerDecision | None:
    """Canonicalize a freshly re-read owner decision; an unresolvable target becomes ``None`` (not conflict)."""
    if decision is None:
        return None
    decision_id, action, target_id = decision
    if target_id is not None:
        try:
            target_id = await entities.resolve_canonical_entity_id(
                session,
                target_id,
                scope=claim.scope,
                multi_workspace_enabled=claim.multi_workspace_enabled,
            )
        except (LookupError, ValueError):
            target_id = None
    return decision_id, action, target_id


def _owner_override(
    owner_decision: _OwnerDecision,
    current_resolution: str,
    current_id: str | None,
    possible: list[str],
) -> tuple[_Plan | None, str, str | None]:
    """Apply a recorded owner decision to a refreshed resolution.

    Returns ``(final_plan, resolution, id)``: a non-``None`` plan ends the candidate's refresh (suppressed or
    conflicting corrections); otherwise the resolution is forced to the assigned entity.
    """
    _, action, assigned_id = owner_decision
    if action == "suppress":
        return ("review", None, [], "owner_suppressed_candidate"), current_resolution, current_id
    if (
        action != "assign"
        or assigned_id is None
        or (current_resolution == "matched" and current_id != str(assigned_id))
    ):
        return (
            ("review", None, possible, "conflicting_owner_corrections"),
            current_resolution,
            current_id,
        )
    return None, "matched", str(assigned_id)


def _recompute_plan_entry(
    candidate: ExtractedEntity,
    known: list[dict[str, object]],
    old: _Plan,
    owner_decision: _OwnerDecision | None,
    locked_refs: dict[UUID, Any],
) -> _Plan:
    """Re-resolve one candidate after the entity lock closure and return its final plan entry (pure).

    Any change from the pre-lock outcome, or a matched entity whose locked type/revision no longer agrees with
    the refreshed listing, becomes review-only; a late entity lock is never acquired out of order.
    """
    old_resolution, old_id, _old_possible, old_reason = old
    current_resolution, current_id, possible = resolve_candidate(
        candidate.name, candidate.type, known
    )
    if owner_decision is not None:
        final, current_resolution, current_id = _owner_override(
            owner_decision,
            current_resolution,
            current_id,
            possible,
        )
        if final is not None:
            return final
    if old_resolution == "review":
        return "review", None, possible, old_reason or "ambiguous_identity"
    if current_resolution != old_resolution or current_id != old_id:
        ids = possible + ([current_id] if current_id else []) + ([old_id] if old_id else [])
        return "review", None, sorted(set(ids)), "identity_changed_during_resolution"
    if current_resolution == "matched":
        identifier = UUID(current_id) if current_id else None
        current = next((item for item in known if str(item["id"]) == current_id), None)
        locked = locked_refs.get(identifier) if identifier else None
        if (
            current is None
            or locked is None
            or locked.type != candidate.type
            or locked.revision != current.get("revision")
        ):
            return (
                "review",
                None,
                [current_id] if current_id else [],
                "identity_changed_during_resolution",
            )
    return current_resolution, current_id, possible, None


async def _refresh_plan(
    session: AsyncSession,
    snap: _Snapshot,
    candidates: list[ExtractedEntity],
    plan: dict[str, _Plan],
    names_by_type: dict[str, list[str]],
    bindings: dict[str, tuple[str, set[UUID]]],
    canonical_decisions: dict[str, _OwnerDecision | None],
    locked_refs: dict[UUID, Any],
) -> None:
    """Refresh names, types, revisions and exact confirmed aliases after the stable sorted entity lock closure.

    A changed outcome becomes review-only (updates ``plan`` in place). Re-reads owner decisions FOR UPDATE so
    a concurrent correction cannot slip between resolution and publication. Never locks entities here.
    """
    claim = snap.claim
    refreshed = await _list_known_candidates(session, claim, names_by_type)
    current_decisions = await entities.get_document_correction_decisions(
        session,
        snap.data.document_id,
        claim.version_id,
        bindings,
        for_update=True,
        scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    for candidate in candidates:
        old = plan[candidate.key]
        if old[3] == "resolution_context_bounded":
            continue
        known, overflow = refreshed[candidate.type]
        if overflow:
            plan[candidate.key] = ("review", None, [], "resolution_context_bounded")
            continue
        current_owner = await _current_owner_decision(
            session, claim, current_decisions.get(candidate.key)
        )
        if current_owner != canonical_decisions[candidate.key]:
            plan[candidate.key] = ("review", None, [], "identity_changed_during_resolution")
            continue
        plan[candidate.key] = _recompute_plan_entry(
            candidate,
            known,
            old,
            canonical_decisions[candidate.key],
            locked_refs,
        )


async def _resolve_entities(
    session: AsyncSession,
    snap: _Snapshot,
    extracted: ExtractionOutput,
) -> tuple[dict[str, _Plan], dict[str, str]]:
    """Resolve every extracted candidate to new / matched / review; returns ``(plan, match_fingerprints)``.

    Ordering: list candidates -> pre-resolve near-duplicates -> owner decisions -> lock matched entities in
    sorted id order (the only entity locks) -> refresh and compare. All work shares the caller's transaction.
    """
    claim, candidates = snap.claim, extracted.entities
    names_by_type: dict[str, list[str]] = {}
    for candidate in candidates:
        names_by_type.setdefault(candidate.type, []).append(candidate.name)
    plan = _initial_plan(candidates, await _list_known_candidates(session, claim, names_by_type))
    _pre_resolve_near_duplicates(candidates, plan)
    match_fingerprints = {c.key: candidate_match_fingerprint(c.name, c.type) for c in candidates}
    bindings = {c.key: (match_fingerprints[c.key], set(c.chunk_ids)) for c in candidates}
    initial_decisions = await entities.get_document_correction_decisions(
        session,
        snap.data.document_id,
        claim.version_id,
        bindings,
        scope=claim.scope,
        multi_workspace_enabled=claim.multi_workspace_enabled,
    )
    canonical_decisions = await _apply_owner_decisions(
        session, claim, candidates, plan, initial_decisions
    )
    locked_refs = await _lock_matched_entities(session, claim, plan)
    await _refresh_plan(
        session, snap, candidates, plan, names_by_type, bindings, canonical_decisions, locked_refs
    )
    return plan, match_fingerprints


def _entity_review_item(
    candidate: ExtractedEntity,
    fingerprint: str,
    reason: str | None,
    possible: list[str],
) -> dict[str, object]:
    """Build the owner-review record (with snapshot digest) for an unresolved entity candidate."""
    snapshot: dict[str, object] = {
        "kind": "entity",
        "candidate_id": str(uuid7()),
        "candidate_type": candidate.type,
        "candidate_name": candidate.name,
        "candidate_key": _candidate_key(candidate.type, candidate.key),
        "match_fingerprint": fingerprint,
        "chunk_ids": [str(identifier) for identifier in candidate.chunk_ids],
        "confidence": candidate.confidence,
    }
    _with_digest(snapshot)
    return {**snapshot, "reason": reason or "ambiguous_identity", "possible_entity_ids": possible}
