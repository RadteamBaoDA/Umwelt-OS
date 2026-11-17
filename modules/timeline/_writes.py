"""Owner event create/update/delete with participant and evidence support (private to the timeline package)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import (
    commit_with_replay,
    make_timeline_change,
)
from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    AccessFence,
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.timeline._common import _admit
from modules.timeline._publish import _schedule_temporal_event
from modules.timeline._reads import _event_read, _evidence_rows, _read_evidence_closure
from modules.timeline.models import (
    Event,
    EventAudit,
    EventEvidence,
    EventParticipant,
    EventSuppression,
    ParticipantEvidence,
)
from modules.timeline.schemas import (
    EventCreate,
    EventPatch,
    EventRead,
)


async def _set_participants(
    session: AsyncSession,
    event_id: UUID,
    values: list[Any],
    *,
    origin: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Replace participant links after validating every canonical entity reference."""
    await session.execute(
        delete(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.event_id.in_(
                select(Event.id).where(
                    Event.workspace_id == scope.workspace_id,
                    Event.id == event_id,
                )
            ),
        )
    )
    entity_ids = sorted({item.entity_id for item in values}, key=str)
    refs = (
        await entities.get_entity_refs(
            session,
            entity_ids,
            for_write=True,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if entity_ids
        else []
    )
    if len(refs) != len(entity_ids):
        raise ValueError("participant entity is missing or redirected")
    for item in values:
        session.add(
            EventParticipant(
                event_id=event_id,
                entity_id=item.entity_id,
                role=item.role,
                metadata_json=item.metadata,
                origin=origin,
            )
        )


async def _remove_unsupported_derived_participants(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
) -> None:
    """Remove derived participant roles with no exact surviving event-evidence support.

    Evidence replacement may cascade its participant-support rows. Manual links
    remain owner-authored, while each derived link must retain at least one
    exact supporting chunk from the event's current evidence set.
    """
    unsupported_ids = list(
        (
            await session.scalars(
                select(EventParticipant.id).where(
                    EventParticipant.event_id == event_id,
                    EventParticipant.event_id.in_(
                        select(Event.id).where(
                            Event.workspace_id == scope.workspace_id,
                            Event.id == event_id,
                        )
                    ),
                    EventParticipant.origin == "derived",
                    ~EventParticipant.id.in_(select(ParticipantEvidence.participant_id)),
                )
            )
        ).all()
    )
    if unsupported_ids:
        await session.execute(
            delete(EventParticipant).where(EventParticipant.id.in_(unsupported_ids))
        )


async def create_event(
    session: AsyncSession,
    payload: EventCreate,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead:
    """Create a manual event and flush its exact evidence, participants, and replay atomically.

    This caller-owned HTTP command commits through ``commit_with_replay``;
    reusable support helpers only flush and never commit.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    pairs = list(payload.evidence)
    refs = await _evidence_rows(
        session,
        pairs,
        for_write=bool(pairs),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source_ids = sorted({ref.source_id for ref in refs}, key=str)
    if len(source_ids) > 1:
        raise ValueError("manual event evidence must belong to one source")
    # Resolve owner observation only at creation; derived occurrence is never inferred here.
    event = Event(
        workspace_id=scope.workspace_id,
        source_id=source_ids[0] if source_ids else None,
        type=payload.type,
        subtype=payload.subtype,
        title=payload.title,
        summary=payload.summary,
        importance_score=payload.importance_score,
        confidence=payload.confidence,
        metadata_json=payload.metadata,
        origin="manual",
        date_precision=payload.date_precision,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        occurred_date=payload.occurred_date,
        end_date=payload.end_date,
        occurrence_timezone=payload.occurrence_timezone,
        observed_at=payload.observed_at or datetime.now(UTC),
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
    )
    session.add(event)
    await session.flush()
    for ref in refs:
        session.add(
            EventEvidence(
                workspace_id=scope.workspace_id,
                event_id=event.id,
                source_id=ref.source_id,
                document_id=ref.document_id,
                document_version_id=ref.document_version_id,
                chunk_id=ref.chunk_id,
                version_number=ref.version_number,
                observed_at=ref.observed_at,
                title_snapshot=ref.title,
                url_snapshot=ref.canonical_url,
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
                evidence_metadata={},
            )
        )
    await _set_participants(
        session,
        event.id,
        payload.participants,
        origin="manual",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await _schedule_temporal_event(
        session, event, ["created"], scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await commit_with_replay(
        session,
        [make_timeline_change(event.id, event.revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _event_read(
        session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def _event_support_snapshot(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
) -> tuple[list[Any], list[Any]]:
    """Read the event's evidence and participant identity rows, ordered by ID, for drift comparison."""
    evidence = list(
        (
            await session.execute(
                select(
                    EventEvidence.id,
                    EventEvidence.source_id,
                    EventEvidence.document_id,
                    EventEvidence.document_version_id,
                    EventEvidence.chunk_id,
                )
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event_id,
                )
                .order_by(EventEvidence.id)
            )
        ).all()
    )
    participants = list(
        (
            await session.execute(
                select(
                    EventParticipant.id,
                    EventParticipant.entity_id,
                    EventParticipant.role,
                    EventParticipant.origin,
                )
                .where(
                    EventParticipant.event_id == event_id,
                    EventParticipant.event_id.in_(
                        select(Event.id).where(Event.workspace_id == scope.workspace_id)
                    ),
                )
                .order_by(EventParticipant.id)
            )
        ).all()
    )
    return evidence, participants


async def _lock_correction_support(
    session: AsyncSession,
    event_id: UUID,
    payload: EventPatch,
    initial_participants: list[Any],
    fence: AccessFence,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Lock the correction's evidence closure and canonical participants; returns the new participants.

    Lock order: sources (sorted), documents (sorted, batches of 100), then canonical entities.
    The evidence closure is read before and after the document locks and must be identical;
    participant entities must still be canonical. Raises ``ValueError`` on drift. The caller
    owns the transaction.
    """
    fields = payload.model_fields_set
    old_pairs = [
        (row.document_version_id, row.chunk_id)
        for row in (
            await session.scalars(
                select(EventEvidence)
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event_id,
                )
                .order_by(EventEvidence.id)
            )
        ).all()
        if row.document_version_id is not None and row.chunk_id is not None
    ]
    new_pairs = payload.evidence if "evidence" in fields else None
    all_pairs = list(dict.fromkeys([*old_pairs, *(new_pairs or [])]))
    refs_before = await _read_evidence_closure(
        session, all_pairs, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    source_ids = sorted({item.source_id for item in refs_before}, key=str)
    from modules.sources import public as sources

    for source_id in source_ids:
        if (
            await sources.lock_source(
                session,
                source_id,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                expected_access_fence=fence,
            )
            is None
        ):
            raise ValueError("event evidence source is unavailable")
    document_ids = sorted({item.document_id for item in refs_before}, key=str)
    # Old and replacement support can span 200 owners. Keep the global sorted
    # order across batches and retain every row lock in this same transaction.
    for offset in range(0, len(document_ids), 100):
        await documents.lock_document_ids(
            session,
            document_ids[offset : offset + 100],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    refs_after = await _read_evidence_closure(
        session, all_pairs, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if refs_after != refs_before:
        raise ValueError("stale event evidence closure; retry the correction")
    participant_values = (payload.participants or []) if "participants" in fields else []
    old_entity_ids = {row.entity_id for row in initial_participants}
    entity_ids = sorted(old_entity_ids | {item.entity_id for item in participant_values}, key=str)
    if entity_ids:
        canonical_refs = []
        for offset in range(0, len(entity_ids), 100):
            canonical_refs.extend(
                await entities.get_entity_refs(
                    session,
                    entity_ids[offset : offset + 100],
                    for_write=True,
                    scope=scope,
                    multi_workspace_enabled=multi_workspace_enabled,
                )
            )
        if [item.requested_id for item in canonical_refs] != entity_ids or any(
            item.canonical_id != item.requested_id for item in canonical_refs
        ):
            raise ValueError("participant entity is missing or redirected; retry the correction")
    return participant_values


def _apply_scalar_corrections(event: Event, supplied: dict[str, Any]) -> dict[str, object]:
    """Apply supplied scalar fields to the locked event and return the changed-field map.

    Derived events record each changed field as owner-corrected. The fully merged temporal shape
    is validated through ``EventCreate`` before any durable revision is published (raises
    ``ValueError``/``ValidationError`` for an invalid merge). Mutates ``event`` only; no I/O.
    """
    changed: dict[str, object] = {}
    for field, value in supplied.items():
        if field in {"participants", "evidence"}:
            continue
        column = "metadata_json" if field == "metadata" else field
        old = getattr(event, column)
        if field in {"started_at", "ended_at", "valid_from", "valid_to"}:
            value = _normalize_optional_instant(value)
        if field == "metadata" and value is None:
            value = {}
        if old != value:
            setattr(event, column, value)
            changed[field] = value.isoformat() if isinstance(value, (datetime, date)) else value
            if event.origin == "derived":
                owners = set(event.owner_fields)
                owners.add(field)
                event.owner_fields = sorted(owners)
    # Validate the fully merged temporal shape before any durable revision is published.
    from modules.timeline.schemas import EventCreate

    EventCreate(
        type=event.type,
        subtype=event.subtype,
        title=event.title,
        summary=event.summary,
        importance_score=event.importance_score,
        confidence=event.confidence,
        metadata=event.metadata_json,
        date_precision=event.date_precision,
        started_at=event.started_at,
        ended_at=event.ended_at,
        occurred_date=event.occurred_date,
        end_date=event.end_date,
        occurrence_timezone=event.occurrence_timezone,
        observed_at=event.observed_at,
        valid_from=event.valid_from,
        valid_to=event.valid_to,
    )
    return changed


async def _replace_event_evidence(
    session: AsyncSession,
    event: Event,
    payload: EventPatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[str]:
    """Replace the event's evidence with the patch's pairs (one source only); return their keys.

    Deletes the old rows, inserts the new snapshots, updates ``event.source_id``, prunes derived
    participants that lost support, and marks ``evidence`` owner-corrected on derived events.
    Raises ``ValueError`` when the pairs span several sources. The caller owns the transaction.
    """
    evidence_pairs = payload.evidence or []
    refs = await _evidence_rows(
        session,
        evidence_pairs,
        for_write=bool(evidence_pairs),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    evidence_sources = {ref.source_id for ref in refs}
    if len(evidence_sources) > 1:
        raise ValueError("event evidence must belong to one source")
    await session.execute(
        delete(EventEvidence).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id
        )
    )
    for ref in refs:
        session.add(
            EventEvidence(
                workspace_id=scope.workspace_id,
                event_id=event.id,
                source_id=ref.source_id,
                document_id=ref.document_id,
                document_version_id=ref.document_version_id,
                chunk_id=ref.chunk_id,
                version_number=ref.version_number,
                observed_at=ref.observed_at,
                title_snapshot=ref.title,
                url_snapshot=ref.canonical_url,
                evidence_metadata={},
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
            )
        )
    event.source_id = next(iter(evidence_sources), None)
    if event.origin == "derived":
        await _remove_unsupported_derived_participants(session, event.id, scope=scope)
    keys = [f"{ref.document_version_id}:{ref.chunk_id}" for ref in refs]
    if event.origin == "derived":
        event.owner_fields = sorted(set(event.owner_fields) | {"evidence"})
    return keys


def _require_unchanged_closure(
    event: Event,
    initial: Any,
    expected_revision: int,
    initial_support: tuple[Any, Any],
    current_support: tuple[Any, Any],
) -> None:
    """Abort a correction when the event or its support closure moved while locks were acquired.

    ``initial`` is the (revision, origin, deleted_at) row read before locking; the support tuples
    are (evidence, participants) snapshots before and after locking. Also enforces the caller's
    ``expected_revision``. Pure; raises ``ValueError`` so the caller aborts before any mutation.
    """
    if (
        event.revision != initial.revision
        or event.origin != initial.origin
        or event.deleted_at != initial.deleted_at
        or current_support != initial_support
        or event.revision != expected_revision
    ):
        raise ValueError("stale event revision or support closure; retry the correction")


async def _apply_relation_corrections(
    session: AsyncSession,
    event: Event,
    payload: EventPatch,
    participant_values: list[Any],
    changed: dict[str, Any],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Apply the participant and evidence parts of a correction and record them in ``changed``.

    Participants are replaced as manual links (a derived event also pins ``participants`` as an
    owner field so re-extraction keeps it); evidence is replaced from the patch. Each part runs only
    when the patch explicitly set it. Mutates ``changed`` in place; the caller owns the transaction.
    """
    fields = payload.model_fields_set
    if "participants" in fields:
        await _set_participants(
            session,
            event.id,
            participant_values,
            origin="manual",
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        changed["participants"] = [item.model_dump(mode="json") for item in participant_values]
        if event.origin == "derived":
            event.owner_fields = sorted(set(event.owner_fields) | {"participants"})
    if "evidence" in fields:
        changed["evidence"] = await _replace_event_evidence(
            session,
            event,
            payload,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )


def _add_correction_audit(
    session: AsyncSession,
    event: Event,
    reason: str | None,
    prior_revision: int,
    changed: dict[str, Any],
    *,
    scope: Scope,
) -> None:
    """Stage the append-only ``EventAudit`` row for an owner correction (no flush or commit).

    ``event.revision`` must already be the resulting revision; the row is committed atomically
    with the correction by the caller's ``commit_with_replay``.
    """
    session.add(
        EventAudit(
            workspace_id=scope.workspace_id,
            event_id=event.id,
            actor_id=_actor(scope),
            reason=reason,
            prior_revision=prior_revision,
            resulting_revision=event.revision,
            changed_json=changed,
        )
    )


async def update_event(
    session: AsyncSession,
    event_id: UUID,
    payload: EventPatch,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead | None:
    """Apply a revision-fenced owner correction after locking its immutable support closure.

    Locks source, document, canonical participant and event rows in that order.
    Any revision or support change while those locks are acquired aborts before
    mutation; the caller commits the correction, audit and replay atomically.
    Explicit null clears nullable fields, participant links, or evidence lists.
    """
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    initial = (
        await session.execute(
            select(Event.revision, Event.origin, Event.deleted_at).where(
                Event.workspace_id == scope.workspace_id,
                Event.id == event_id,
                Event.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if initial is None:
        return None
    initial_evidence, initial_participants = await _event_support_snapshot(
        session, event_id, scope=scope
    )
    supplied = payload.model_dump(
        exclude_unset=True, exclude={"expected_revision", "reason", "participants", "evidence"}
    )
    participant_values = await _lock_correction_support(
        session,
        event_id,
        payload,
        initial_participants,
        fence,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    event = await session.scalar(
        select(Event)
        .where(Event.workspace_id == scope.workspace_id, Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or event.deleted_at is not None:
        return None
    current_evidence, current_participants = await _event_support_snapshot(
        session, event_id, scope=scope
    )
    _require_unchanged_closure(
        event,
        initial,
        payload.expected_revision,
        (initial_evidence, initial_participants),
        (current_evidence, current_participants),
    )
    prior = event.revision
    changed = _apply_scalar_corrections(event, supplied)
    await _apply_relation_corrections(
        session,
        event,
        payload,
        participant_values,
        changed,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if not changed:
        raise ValueError("event correction contains no changes")
    event.revision += 1
    event.updated_at = datetime.now(UTC)
    _add_correction_audit(session, event, payload.reason, prior, changed, scope=scope)
    await _schedule_temporal_event(
        session, event, list(changed), scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    await commit_with_replay(
        session,
        [make_timeline_change(event.id, event.revision, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return await _event_read(
        session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


def _normalize_optional_instant(value: Any) -> Any:
    """Normalize explicit-offset datetimes for merged PATCH payload values."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include an explicit UTC offset")
        return value.astimezone(UTC)
    return value


async def delete_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    expected_revision: int,
    reason: str,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Tombstone a revision-fenced event and suppress its exact derived proposal before replay commit."""
    fence = await _admit(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True
    )
    event = await session.scalar(
        select(Event)
        .where(
            Event.workspace_id == scope.workspace_id,
            Event.id == event_id,
        )
        .with_for_update()
    )
    if event is None or event.deleted_at is not None:
        return False
    if event.revision != expected_revision:
        raise ValueError("event revision is stale")
    if event.origin == "derived":
        supports = list(
            (
                await session.scalars(
                    select(EventEvidence).where(
                        EventEvidence.workspace_id == scope.workspace_id,
                        EventEvidence.event_id == event.id,
                        EventEvidence.document_version_id.is_not(None),
                        EventEvidence.candidate_hash.is_not(None),
                    )
                )
            ).all()
        )
        unique_supports = {
            (item.document_version_id, item.candidate_hash): item for item in supports
        }
        for item in unique_supports.values():
            session.add(
                EventSuppression(
                    workspace_id=scope.workspace_id,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    source_id=item.source_id,
                    source_generation=item.source_generation,
                    candidate_hash=item.candidate_hash,
                )
            )
    prior = event.revision
    event.deleted_at = datetime.now(UTC)
    event.revision += 1
    session.add(
        EventAudit(
            workspace_id=scope.workspace_id,
            event_id=event.id,
            actor_id=_actor(scope),
            reason=reason,
            prior_revision=prior,
            resulting_revision=event.revision,
            changed_json={"deleted": True},
        )
    )
    await _schedule_temporal_event(
        session,
        event,
        ["deleted"],
        deleted=True,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    await commit_with_replay(
        session,
        [make_timeline_change(event.id, event.revision, deleted=True, scope=scope)],
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=fence,
    )
    return True
