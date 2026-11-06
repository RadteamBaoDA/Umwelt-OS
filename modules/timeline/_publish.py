"""Derived-event publication from extraction proposals and provider events (private to the timeline package)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.timeline._common import _admit
from modules.timeline._extraction_work import finish_extraction_work
from modules.timeline._reads import _read_evidence_closure
from modules.timeline.models import (
    Event,
    EventEvidence,
    EventParticipant,
    EventSuppression,
    ParticipantEvidence,
)
from modules.timeline.schemas import (
    EventCreate,
)


def _proposal_participants(
    proposal: Any,
    memberships: list[Any],
    membership_by_id: dict[UUID, Any],
) -> dict[tuple[UUID, str], Any]:
    """Resolve a proposal's participants to canonical ``(entity, role)`` keys, merging duplicate roles.

    Every participant must cite a known membership, a subset of the event's evidence, and
    exactly the chunks that canonical entity is a member of; otherwise ``ValueError``. Pure.
    """
    participants: dict[tuple[UUID, str], Any] = {}
    for participant in proposal.participants:
        membership = membership_by_id.get(participant.membership_id)
        if membership is None or membership.chunk_id not in participant.chunk_ids:
            raise ValueError("model returned an unknown participant membership")
        if not set(participant.chunk_ids) <= set(proposal.evidence_chunk_ids):
            raise ValueError("participant support must be a subset of event evidence")
        exact = {
            item.chunk_id
            for item in memberships
            if item.entity_id == membership.entity_id and item.chunk_id in participant.chunk_ids
        }
        if exact != set(participant.chunk_ids):
            raise ValueError("participant role lacks exact canonical membership support")
        key = (membership.entity_id, participant.role)
        previous = participants.get(key)
        if previous is not None:
            participant = previous.model_copy(
                update={
                    "chunk_ids": sorted(
                        set(previous.chunk_ids) | set(participant.chunk_ids), key=str
                    ),
                }
            )
        participants[key] = participant
    return participants


def _candidate_hash(proposal: Any, participants: dict[tuple[UUID, str], Any]) -> str:
    """Hash the proposal's canonical identity (type, title, occurrence, participants, evidence). Pure."""
    from hashlib import sha256

    participant_identity = [
        (str(entity_id), role, sorted(map(str, item.chunk_ids)))
        for (entity_id, role), item in participants.items()
    ]
    data = proposal.model_dump(mode="json")
    fingerprint = {
        "type": proposal.type,
        "title": proposal.title,
        "occurrence": [
            data.get(name)
            for name in ("date_precision", "started_at", "ended_at", "occurred_date", "end_date")
        ],
        "participants": sorted(participant_identity),
        "evidence": sorted(map(str, proposal.evidence_chunk_ids)),
    }
    return sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_proposals(
    proposals: Any,
    chunk_ids: list[UUID],
    memberships: list[Any],
    membership_by_id: dict[UUID, Any],
) -> tuple[list[tuple[Any, str, dict[tuple[UUID, str], Any]]], set[UUID]]:
    """Validate proposals against the allowed evidence and coalesce equal candidates.

    Returns ``(normalized, entity_ids)`` where candidates sharing a canonical hash keep the one
    with the smallest canonical JSON (deterministic), ordered by hash, and ``entity_ids`` are
    every canonical participant entity. Raises ``ValueError`` on unknown evidence. Pure.
    """
    allowed = set(chunk_ids)
    normalized: list[tuple[Any, str, dict[tuple[UUID, str], Any]]] = []
    entity_ids: set[UUID] = set()
    for proposal in proposals.events:
        if not set(proposal.evidence_chunk_ids) <= allowed:
            raise ValueError("model returned unknown event evidence")
        participants = _proposal_participants(proposal, memberships, membership_by_id)
        entity_ids.update(entity_id for entity_id, _role in participants)
        normalized.append((proposal, _candidate_hash(proposal, participants), participants))
    unique_by_candidate: dict[str, tuple[Any, str, dict[tuple[UUID, str], Any]]] = {}
    for item in normalized:
        previous = unique_by_candidate.get(item[1])
        item_json = json.dumps(
            item[0].model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        previous_json = (
            json.dumps(previous[0].model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            if previous
            else ""
        )
        if previous is None or item_json < previous_json:
            unique_by_candidate[item[1]] = item
    return [unique_by_candidate[key] for key in sorted(unique_by_candidate)], entity_ids


async def _lock_extraction_entities(
    session: AsyncSession,
    entity_ids: set[UUID],
    proposals: Any,
    membership_by_id: dict[UUID, Any],
    membership_revisions: dict[UUID, int],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Lock participant entities (sorted) and require the pre-inference canonical identity and revision.

    No graph lock is held during provider inference; sorted canonical entity revisions and then
    events are acquired only after a validated response exists. Raises ``ValueError`` on drift.
    """
    if not entity_ids:
        return
    entity_refs = await entities.get_entity_refs(
        session,
        sorted(entity_ids, key=str),
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if {item.canonical_id for item in entity_refs} != entity_ids:
        raise ValueError("participant canonical identity changed during extraction")
    expected_revisions = {
        membership_by_id[participant.membership_id].entity_id: membership_revisions[
            participant.membership_id
        ]
        for proposal in proposals.events
        for participant in proposal.participants
    }
    if any(item.revision != expected_revisions.get(item.canonical_id) for item in entity_refs):
        raise ValueError("participant canonical revision changed during extraction")


def _event_fields_from_proposal(proposal: Any) -> dict[str, Any]:
    """Return the refreshable event columns of a proposal, with instants normalized to UTC. Pure."""
    return {
        "title": proposal.title,
        "summary": proposal.summary,
        "type": proposal.type,
        "subtype": proposal.subtype,
        "importance_score": proposal.importance_score,
        "confidence": proposal.confidence,
        "date_precision": proposal.date_precision,
        "started_at": proposal.started_at.astimezone(UTC) if proposal.started_at else None,
        "ended_at": proposal.ended_at.astimezone(UTC) if proposal.ended_at else None,
        "occurred_date": proposal.occurred_date,
        "end_date": proposal.end_date,
        "occurrence_timezone": proposal.occurrence_timezone,
        "valid_from": proposal.valid_from.astimezone(UTC) if proposal.valid_from else None,
        "valid_to": proposal.valid_to.astimezone(UTC) if proposal.valid_to else None,
    }


def _refresh_derived_event(
    event: Event,
    proposal: Any,
    candidate_hash: str,
    refresh_conflicts: dict[str, list[str]],
) -> None:
    """Refresh an existing derived event from a new proposal without touching owner-corrected fields.

    Owner fields always win. When the merged temporal shape would be invalid, the unowned
    temporal group is skipped and recorded in ``refresh_conflicts`` while safe fields still
    refresh; a non-temporal validation failure re-raises. A real change bumps the revision.
    Mutates ``event`` and ``refresh_conflicts`` only; no I/O.
    """
    owners = set(event.owner_fields)
    refreshed = _event_fields_from_proposal(proposal)
    pending_updates = {
        field: value
        for field, value in refreshed.items()
        if field not in owners and getattr(event, field) != value
    }
    base_values = {
        "type": event.type,
        "subtype": event.subtype,
        "title": event.title,
        "summary": event.summary,
        "importance_score": event.importance_score,
        "confidence": event.confidence,
        "metadata": event.metadata_json,
        "date_precision": event.date_precision,
        "started_at": event.started_at,
        "ended_at": event.ended_at,
        "occurred_date": event.occurred_date,
        "end_date": event.end_date,
        "occurrence_timezone": event.occurrence_timezone,
        "observed_at": event.observed_at,
        "valid_from": event.valid_from,
        "valid_to": event.valid_to,
    }
    temporal_fields = {
        "date_precision",
        "started_at",
        "ended_at",
        "occurred_date",
        "end_date",
        "occurrence_timezone",
        "valid_from",
        "valid_to",
    }
    candidate_values = {**base_values, **pending_updates}
    try:
        EventCreate(**candidate_values)
    except ValidationError:
        temporal_conflicts = sorted(set(pending_updates) & temporal_fields)
        if not temporal_conflicts:
            raise
        # Retain the previously valid owner-corrected temporal shape;
        # a conflicting unowned boundary is not allowed to replace it.
        pending_updates = {
            field: value for field, value in pending_updates.items() if field not in temporal_fields
        }
        candidate_values = {**base_values, **pending_updates}
        EventCreate(**candidate_values)
        refresh_conflicts[candidate_hash] = temporal_conflicts
    changed = bool(pending_updates)
    for field, value in pending_updates.items():
        setattr(event, field, value)
    if changed:
        event.revision += 1
        event.updated_at = datetime.now(UTC)


async def _insert_derived_evidence(
    session: AsyncSession,
    event: Event,
    proposal: Any,
    candidate_hash: str,
    evidence_by_chunk: dict[UUID, Any],
    ready: Any,
    identity: str,
    *,
    scope: Scope,
) -> None:
    """Insert the proposal's evidence snapshots in one idempotent executemany.

    The unique constraint keeps retries idempotent exactly as a per-row insert would; one
    round trip per event instead of one per chunk. Flush-only; the caller owns the transaction.
    """
    from sqlalchemy.dialects.postgresql import insert

    extracted_at = datetime.now(UTC)
    evidence_values = []
    for chunk_id in proposal.evidence_chunk_ids:
        ref = evidence_by_chunk[chunk_id]
        evidence_values.append(
            {
                "workspace_id": scope.workspace_id,
                "event_id": event.id,
                "source_id": ref.source_id,
                "document_id": ref.document_id,
                "document_version_id": ref.document_version_id,
                "chunk_id": ref.chunk_id,
                "version_number": ref.version_number,
                "source_generation": ready.source_generation,
                "extraction_identity": identity,
                "candidate_hash": candidate_hash,
                "confidence": proposal.confidence,
                "extracted_at": extracted_at,
                "observed_at": ref.observed_at,
                "title_snapshot": ref.title,
                "url_snapshot": ref.canonical_url,
                "evidence_metadata": {},
                "excerpt": ref.excerpt,
                "metadata_is_version_snapshot": ref.metadata_is_version_snapshot,
            }
        )
    if evidence_values:
        await session.execute(
            insert(EventEvidence).on_conflict_do_nothing(constraint="uq_timeline_event_evidence"),
            evidence_values,
        )


async def _link_derived_participants(
    session: AsyncSession,
    event: Event,
    participants: dict[tuple[UUID, str], Any],
    *,
    scope: Scope,
) -> None:
    """Upsert each derived participant row (locked) and link it to its exact evidence rows idempotently."""
    from sqlalchemy.dialects.postgresql import insert

    for (entity_id, _role), participant in participants.items():
        row = await session.scalar(
            select(EventParticipant)
            .where(
                EventParticipant.event_id == event.id,
                EventParticipant.event_id.in_(
                    select(Event.id).where(Event.workspace_id == scope.workspace_id)
                ),
                EventParticipant.entity_id == entity_id,
                EventParticipant.role == participant.role,
            )
            .with_for_update()
        )
        if row is None:
            row = EventParticipant(
                event_id=event.id,
                entity_id=entity_id,
                role=participant.role,
                metadata_json={},
                origin="derived",
            )
            session.add(row)
            await session.flush()
        rows = (
            await session.scalars(
                select(EventEvidence).where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                    EventEvidence.chunk_id.in_(participant.chunk_ids),
                )
            )
        ).all()
        if rows:
            await session.execute(
                insert(ParticipantEvidence).on_conflict_do_nothing(
                    constraint="uq_timeline_participant_evidence"
                ),
                [
                    {"participant_id": row.id, "event_evidence_id": evidence_row.id}
                    for evidence_row in rows
                ],
            )


def _new_derived_event(
    proposal: Any, candidate_hash: str, ready: Any, identity: str, *, scope: Scope
) -> Event:
    """Build an unflushed derived event row from a proposal and the ready version. Pure."""
    return Event(
        workspace_id=scope.workspace_id,
        source_id=ready.source_id,
        type=proposal.type,
        subtype=proposal.subtype,
        title=proposal.title,
        summary=proposal.summary,
        importance_score=proposal.importance_score,
        confidence=proposal.confidence,
        metadata_json={},
        origin="derived",
        date_precision=proposal.date_precision,
        started_at=proposal.started_at.astimezone(UTC) if proposal.started_at else None,
        ended_at=proposal.ended_at.astimezone(UTC) if proposal.ended_at else None,
        occurred_date=proposal.occurred_date,
        end_date=proposal.end_date,
        occurrence_timezone=proposal.occurrence_timezone,
        observed_at=ready.observed_at,
        valid_from=proposal.valid_from.astimezone(UTC) if proposal.valid_from else None,
        valid_to=proposal.valid_to.astimezone(UTC) if proposal.valid_to else None,
        extraction_identity=identity,
        candidate_hash=candidate_hash,
    )


async def publish_extracted_events(
    session: AsyncSession,
    *,
    work_id: UUID,
    lease_owner: str,
    ready: Any,
    proposals: Any,
    model: str | None,
    membership_revisions: dict[UUID, int],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Publish bounded event proposals and exact evidence under caller-owned source/document fences.

    Resolves and locks participant entities after inference, then event rows.
    Membership revisions captured before inference must still match the locked
    canonical revisions. Existing owner fields win; if proposed unowned
    temporal fields conflict with that merged state, the unowned temporal
    refresh group is skipped and named while safe fields, evidence, and results
    publish.
    It flushes only; the worker commits results, replay and success atomically.
    Returns ``False`` (nothing published) when the lease or its version/generation is lost.
    """
    from modules.timeline.extraction import EXTRACTOR_VERSION, PROMPT_VERSION
    from modules.timeline.models import TimelineExtractionWork

    work = await session.scalar(
        select(TimelineExtractionWork)
        .where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id,
            TimelineExtractionWork.status == "running",
            TimelineExtractionWork.lease_owner == lease_owner,
            TimelineExtractionWork.lease_expires_at > datetime.now(UTC),
        )
        .with_for_update()
    )
    if (
        work is None
        or work.source_generation != ready.source_generation
        or work.document_version_id != ready.document_version_id
    ):
        return False
    chunk_ids = sorted(
        {chunk for item in proposals.events for chunk in item.evidence_chunk_ids}, key=str
    )
    refs = (
        await documents.read_extraction_evidence_refs(
            session,
            document_id=ready.document_id,
            document_version_id=ready.document_version_id,
            source_id=ready.source_id,
            source_generation=ready.source_generation,
            chunk_ids=chunk_ids,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if chunk_ids
        else []
    )
    if refs is None:
        raise ValueError("event evidence is no longer current")
    evidence = await _read_evidence_closure(
        session,
        [(item.document_version_id, item.chunk_id) for item in refs],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(evidence) != len(refs):
        raise ValueError("event evidence snapshot is unavailable")
    evidence_by_chunk = {item.chunk_id: item for item in evidence}
    memberships = []
    for offset in range(0, len(chunk_ids), 100):
        memberships.extend(
            await entities.list_version_membership_refs(
                session,
                ready.document_version_id,
                chunk_ids[offset : offset + 100],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    membership_by_id = {item.membership_id: item for item in memberships}
    identity = f"{ready.document_version_id}:{ready.source_generation}:{EXTRACTOR_VERSION}:{PROMPT_VERSION}"
    normalized, entity_ids = _normalize_proposals(
        proposals, chunk_ids, memberships, membership_by_id
    )
    await _lock_extraction_entities(
        session,
        entity_ids,
        proposals,
        membership_by_id,
        membership_revisions,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    candidates = [item[1] for item in normalized]
    existing = (
        list(
            (
                await session.scalars(
                    select(Event)
                    .where(
                        Event.workspace_id == scope.workspace_id,
                        Event.extraction_identity == identity,
                        Event.candidate_hash.in_(candidates),
                    )
                    .order_by(Event.id)
                    .with_for_update()
                )
            ).all()
        )
        if candidates
        else []
    )
    by_hash = {item.candidate_hash: item for item in existing}
    suppressed = (
        set(
            (
                await session.scalars(
                    select(EventSuppression.candidate_hash).where(
                        EventSuppression.workspace_id == scope.workspace_id,
                        EventSuppression.document_version_id == ready.document_version_id,
                        EventSuppression.candidate_hash.in_(candidates),
                    )
                )
            ).all()
        )
        if candidates
        else set()
    )
    published: list[tuple[Event, Any]] = []
    refresh_conflicts: dict[str, list[str]] = {}
    for proposal, candidate_hash, participants in normalized:
        if candidate_hash in suppressed:
            continue
        event = by_hash.get(candidate_hash)
        if event is None:
            event = _new_derived_event(proposal, candidate_hash, ready, identity, scope=scope)
            session.add(event)
            await session.flush()
        else:
            _refresh_derived_event(event, proposal, candidate_hash, refresh_conflicts)
        published.append((event, proposal))
        if "evidence" not in event.owner_fields:
            await _insert_derived_evidence(
                session,
                event,
                proposal,
                candidate_hash,
                evidence_by_chunk,
                ready,
                identity,
                scope=scope,
            )
        if "participants" not in event.owner_fields and "evidence" not in event.owner_fields:
            await _link_derived_participants(session, event, participants, scope=scope)
    await session.flush()
    proposals_json = [
        {
            "event_id": str(event.id),
            "candidate_hash": event.candidate_hash,
            "proposal": proposal.model_dump(mode="json"),
            "support": {
                "document_version_id": str(ready.document_version_id),
                "chunk_ids": [str(item) for item in proposal.evidence_chunk_ids],
                "membership_ids": [str(item.membership_id) for item in proposal.participants],
            },
            "refresh_conflicts": refresh_conflicts.get(str(event.candidate_hash), []),
        }
        for event, proposal in published
    ]
    if not await finish_extraction_work(
        session,
        work_id,
        lease_owner,
        proposals_json,
        model,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    ):
        return False
    for event, _proposal in published:
        await _schedule_temporal_event(
            session,
            event,
            ["extracted"],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return True


async def summarize_source_events(
    session: AsyncSession,
    source_id: UUID,
    type_prefix: str,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[dict[str, int], datetime | None]:
    """Count visible derived events per type for one source and return the latest observation time.

    Applies the same visibility rule as timeline listing (not deleted, backed by exact evidence)
    so counts never exceed what the Timeline can show. Read-only; admits the owner workspace scope before reading.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    escaped = type_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await session.execute(
            select(Event.type, func.count(), func.max(Event.observed_at))
            .where(
                Event.workspace_id == scope.workspace_id,
                Event.source_id == source_id,
                Event.deleted_at.is_(None),
                Event.origin == "derived",
                Event.type.like(f"{escaped}%", escape="\\"),
                Event.id.in_(
                    select(EventEvidence.event_id).where(
                        EventEvidence.workspace_id == scope.workspace_id,
                        EventEvidence.document_version_id.is_not(None),
                        EventEvidence.chunk_id.is_not(None),
                    )
                ),
            )
            .group_by(Event.type)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}, max((row[2] for row in rows), default=None)


async def publish_provider_event(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    document_id: UUID,
    document_version_id: UUID,
    chunk_ids: list[UUID],
    extraction_identity: str,
    record_key: str,
    event_type: str,
    title: str,
    summary: str | None,
    started_at: datetime,
    observed_at: datetime,
    metadata: dict[str, Any],
    participants: list[tuple[UUID, str]],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID | None:
    """Idempotently publish one deterministic provider event with exact current evidence.

    The event identity is ``(extraction_identity, sha256(record_key))``, so every
    version of the same provider record updates one event instead of creating
    duplicates. Owner-owned fields are never overwritten, owner-deleted events
    stay deleted, and the revision/temporal change is scheduled only when a
    field, the evidence set or a participant actually changed. The caller holds
    the source/document fences and the outer transaction; this only flushes.
    Returns None when the evidence is no longer the current ready version.
    """
    from hashlib import sha256

    from sqlalchemy.dialects.postgresql import insert

    refs = await documents.read_extraction_evidence_refs(
        session,
        document_id=document_id,
        document_version_id=document_version_id,
        source_id=source_id,
        source_generation=source_generation,
        chunk_ids=chunk_ids,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if refs is None:
        return None
    evidence = await _read_evidence_closure(
        session,
        [(item.document_version_id, item.chunk_id) for item in refs],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(evidence) != len(refs):
        return None
    canonical = await entities.get_entity_refs(
        session,
        [entity_id for entity_id, _ in participants],
        for_write=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    candidate_hash = sha256(record_key.encode("utf-8")).hexdigest()
    event = await session.scalar(
        select(Event)
        .where(
            Event.workspace_id == scope.workspace_id,
            Event.extraction_identity == extraction_identity,
            Event.candidate_hash == candidate_hash,
        )
        .with_for_update()
    )
    if event is not None and event.deleted_at is not None:
        return None
    changed = event is None
    if event is None:
        event = Event(
            workspace_id=scope.workspace_id,
            source_id=source_id,
            type=event_type,
            title=title[:300],
            summary=summary,
            confidence=1.0,
            metadata_json=metadata,
            origin="derived",
            date_precision="timed",
            started_at=started_at,
            observed_at=observed_at,
            extraction_identity=extraction_identity,
            candidate_hash=candidate_hash,
        )
        session.add(event)
        await session.flush()
    else:
        owners = set(event.owner_fields)
        desired = {
            "title": title[:300],
            "summary": summary,
            "type": event_type,
            "started_at": started_at,
            "observed_at": observed_at,
            "metadata_json": metadata,
        }
        updates = {
            name: value
            for name, value in desired.items()
            if name not in owners and getattr(event, name) != value
        }
        for name, value in updates.items():
            setattr(event, name, value)
        if updates:
            event.revision += 1
            event.updated_at = datetime.now(UTC)
            changed = True
    for item in evidence:
        inserted = await session.execute(
            insert(EventEvidence)
            .values(
                workspace_id=scope.workspace_id,
                event_id=event.id,
                source_id=item.source_id,
                document_id=item.document_id,
                document_version_id=item.document_version_id,
                chunk_id=item.chunk_id,
                version_number=item.version_number,
                source_generation=source_generation,
                extraction_identity=extraction_identity,
                candidate_hash=candidate_hash,
                confidence=1.0,
                extracted_at=datetime.now(UTC),
                observed_at=item.observed_at,
                title_snapshot=item.title,
                url_snapshot=item.canonical_url,
                evidence_metadata={},
                excerpt=item.excerpt,
                metadata_is_version_snapshot=item.metadata_is_version_snapshot,
            )
            .on_conflict_do_nothing(constraint="uq_timeline_event_evidence")
            .returning(EventEvidence.id)
        )
        changed = changed or inserted.first() is not None
    evidence_rows = list(
        (
            await session.scalars(
                select(EventEvidence).where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                    EventEvidence.chunk_id.in_(chunk_ids),
                    EventEvidence.document_version_id == document_version_id,
                )
            )
        ).all()
    )
    for ref, (_, role) in zip(canonical, participants, strict=False):
        row = await session.scalar(
            select(EventParticipant)
            .where(
                EventParticipant.event_id == event.id,
                EventParticipant.entity_id == ref.canonical_id,
                EventParticipant.role == role,
            )
            .with_for_update()
        )
        if row is None:
            row = EventParticipant(
                event_id=event.id,
                entity_id=ref.canonical_id,
                role=role,
                metadata_json={},
                origin="derived",
            )
            session.add(row)
            await session.flush()
            changed = True
        for item in evidence_rows:
            await session.execute(
                insert(ParticipantEvidence)
                .values(
                    participant_id=row.id,
                    event_evidence_id=item.id,
                )
                .on_conflict_do_nothing(constraint="uq_timeline_participant_evidence")
            )
    await session.flush()
    if changed:
        await _schedule_temporal_event(
            session,
            event,
            ["extracted"],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    return event.id


async def _schedule_temporal_event(
    session: AsyncSession,
    event: Event,
    fields: list[str],
    *,
    deleted: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Flush desired temporal history with the canonical event transaction, preserving exact evidence IDs."""
    from modules.knowledge.temporal import public as temporal

    rows = (
        await session.scalars(
            select(EventEvidence).where(
                EventEvidence.workspace_id == scope.workspace_id,
                EventEvidence.event_id == event.id,
            )
        )
    ).all()
    await temporal.schedule_canonical_change(
        session,
        kind="event",
        canonical_id=event.id,
        revision=event.revision,
        fields=fields,
        support=[
            (item.document_version_id, item.chunk_id)
            for item in rows
            if item.document_version_id is not None and item.chunk_id is not None
        ],
        origin=event.origin,
        deleted=deleted,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def temporal_event_refs(
    session: AsyncSession,
    version_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[dict[str, Any]]:
    """Expose complete bounded canonical event/support identity for selected temporal versions; no private reads by callers."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(set(version_ids)) > 100:
        raise ValueError("Temporal event scope exceeds100 versions")
    rows = (
        await session.execute(
            select(Event, EventEvidence)
            .join(
                EventEvidence,
                EventEvidence.event_id == Event.id,
            )
            .where(
                Event.workspace_id == scope.workspace_id,
                EventEvidence.workspace_id == scope.workspace_id,
                EventEvidence.document_version_id.in_(version_ids),
                Event.deleted_at.is_(None),
            )
            .order_by(Event.id, EventEvidence.id)
            .limit(10001)
        )
    ).all()
    if len(rows) > 10000:
        raise ValueError("Temporal event support closure exceeds10000 references")
    return [
        {
            "event_id": event.id,
            "revision": event.revision,
            "origin": event.origin,
            "document_version_id": evidence.document_version_id,
            "chunk_id": evidence.chunk_id,
            "valid_from": event.valid_from,
            "valid_to": event.valid_to,
        }
        for event, evidence in rows
    ]
