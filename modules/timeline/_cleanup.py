"""Support closure, cleanup, entity merge/split and unsupported-event hiding (private to the timeline package)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, ForbiddenError
from core.realtime import (
    ReplayDraft,
    make_timeline_collection_change,
)
from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.schemas import (
    AccessFence,
    Scope,
    WorkspaceContext,
)
from modules.sources.public import SourceFence
from modules.timeline._common import _admit
from modules.timeline._publish import _schedule_temporal_event
from modules.timeline.models import (
    Event,
    EventEvidence,
    EventParticipant,
    ParticipantEvidence,
)


@dataclass(frozen=True)
class TimelineSupportClosure:
    """Workspace-qualified ID closure of timeline support for one Source or Document."""

    source_id: UUID
    document_id: UUID | None
    event_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    participant_ids: tuple[UUID, ...]
    participant_entity_ids: tuple[UUID, ...]
    overflow: bool


_CLEANUP_LIMIT = 10_000


def _require_cleanup_fences(
    actual: AccessFence,
    *,
    closure: TimelineSupportClosure,
    scope: Scope,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Require the held fences to still match a fresh read and the closure; else 409 ``Cleanup authority changed``."""
    if (
        actual != access_fence
        or source_fence.id != closure.source_id
        or source_fence.workspace_id != scope.workspace_id
    ):
        raise ConflictError("Cleanup authority changed")


async def support_cleanup_ids(
    session: AsyncSession,
    *,
    source_id: UUID,
    document_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelineSupportClosure:
    """Discover (nonlocking, bounded, ID-only) the timeline support of one Source or Document.

    Cleanup uses one source-scoped collection invalidation, so event identities
    are not truncated to the realtime per-batch limit.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    workspace_id = scope.workspace_id
    evidence_rows = list(
        (
            await session.execute(
                select(EventEvidence.id, EventEvidence.event_id)
                .where(
                    EventEvidence.workspace_id == workspace_id,
                    EventEvidence.document_id == document_id
                    if document_id is not None
                    else EventEvidence.source_id == source_id,
                )
                .order_by(EventEvidence.id)
                .limit(_CLEANUP_LIMIT + 1)
            )
        ).all()
    )
    overflow = len(evidence_rows) > _CLEANUP_LIMIT
    evidence_rows = evidence_rows[:_CLEANUP_LIMIT]
    evidence_ids = [row[0] for row in evidence_rows]
    event_ids = sorted({row[1] for row in evidence_rows})
    participant_ids: list[UUID] = []
    entity_ids: set[UUID] = set()
    if event_ids:
        if evidence_ids:
            participant_ids = list(
                (
                    await session.scalars(
                        select(EventParticipant.id)
                        .join(
                            ParticipantEvidence,
                            ParticipantEvidence.participant_id == EventParticipant.id,
                        )
                        .join(Event, Event.id == EventParticipant.event_id)
                        .where(
                            Event.workspace_id == workspace_id,
                            ParticipantEvidence.event_evidence_id.in_(evidence_ids),
                        )
                        .distinct()
                        .order_by(EventParticipant.id)
                        .limit(_CLEANUP_LIMIT + 1)
                    )
                ).all()
            )
            overflow |= len(participant_ids) > _CLEANUP_LIMIT
            participant_ids = participant_ids[:_CLEANUP_LIMIT]
        entity_rows = list(
            (
                await session.scalars(
                    select(EventParticipant.entity_id)
                    .join(Event, Event.id == EventParticipant.event_id)
                    .where(
                        Event.workspace_id == workspace_id, EventParticipant.event_id.in_(event_ids)
                    )
                    .distinct()
                    .order_by(EventParticipant.entity_id)
                    .limit(_CLEANUP_LIMIT + 1)
                )
            ).all()
        )
        overflow |= len(entity_rows) > _CLEANUP_LIMIT
        entity_ids.update(entity_rows[:_CLEANUP_LIMIT])
    return TimelineSupportClosure(
        source_id=source_id,
        document_id=document_id,
        event_ids=tuple(event_ids),
        evidence_ids=tuple(evidence_ids),
        participant_ids=tuple(sorted(participant_ids)),
        participant_entity_ids=tuple(sorted(entity_ids)),
        overflow=overflow,
    )


async def prepare_support_cleanup_in_uow(
    session: AsyncSession,
    closure: TimelineSupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> None:
    """Lock events, then evidence, then participants in UUID order; no mutation."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    if closure.overflow:
        raise ValueError("Timeline support cleanup exceeds its atomic limit")
    workspace_id = scope.workspace_id
    if closure.event_ids:
        await session.scalars(
            select(Event.id)
            .where(Event.workspace_id == workspace_id, Event.id.in_(closure.event_ids))
            .order_by(Event.id)
            .with_for_update()
        )
    if closure.evidence_ids:
        await session.scalars(
            select(EventEvidence.id)
            .where(
                EventEvidence.workspace_id == workspace_id,
                EventEvidence.id.in_(closure.evidence_ids),
            )
            .order_by(EventEvidence.id)
            .with_for_update()
        )
    if closure.participant_ids:
        await session.scalars(
            select(EventParticipant.id)
            .where(
                EventParticipant.event_id.in_(
                    select(Event.id).where(Event.workspace_id == workspace_id)
                ),
                EventParticipant.id.in_(closure.participant_ids),
            )
            .order_by(EventParticipant.id)
            .with_for_update()
        )


async def correction_event_ids(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
) -> list[UUID]:
    """Capture bounded event rows containing participants before correction locks are taken.

    The correction transaction already holds owner admission; this read adds only the role guard and workspace scope.
    """
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise ForbiddenError("Workspace owner required")
    if not entity_ids:
        return []
    ids = list(
        (
            await session.scalars(
                select(EventParticipant.event_id)
                .join(
                    Event,
                    Event.id == EventParticipant.event_id,
                )
                .where(
                    Event.workspace_id == scope.workspace_id,
                    EventParticipant.entity_id.in_(entity_ids),
                )
                .distinct()
                .order_by(EventParticipant.event_id)
                .limit(201)
            )
        ).all()
    )
    if len(ids) > 200:
        raise ValueError("Timeline correction closure exceeds 200 participant events")
    return sorted(set(ids), key=str)


async def lock_event_ids(
    session: AsyncSession,
    event_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Acquire captured event row locks in global UUID order after entity and relationship locks."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if event_ids:
        await session.execute(
            select(Event.id)
            .where(
                Event.workspace_id == scope.workspace_id,
                Event.id.in_(sorted(set(event_ids), key=str)),
            )
            .order_by(Event.id)
            .with_for_update()
        )


async def revise_corrected_events(
    session: AsyncSession,
    event_ids: list[UUID],
    *,
    entity_id: UUID,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ReplayDraft]:
    """Bump each changed event once and return one entity-scoped collection invalidation.

    Callers must supply only events whose participant representation changed
    and must already hold their event locks after canonical entity locks.
    """
    changed_ids = sorted(set(event_ids), key=str)
    for event_id in changed_ids:
        event = await session.scalar(
            select(Event).where(
                Event.workspace_id == scope.workspace_id,
                Event.id == event_id,
            )
        )
        if event is not None:
            event.revision += 1
            event.updated_at = datetime.now(UTC)
            await _schedule_temporal_event(
                session,
                event,
                ["participants"],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
    return (
        [make_timeline_collection_change(entity_id=entity_id, scope=scope)] if changed_ids else []
    )


async def apply_entity_merge(
    session: AsyncSession,
    *,
    source_id: UUID,
    target_id: UUID,
    event_ids: list[UUID],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Transfer links through prelocked events, coalesce roles with manual origin winning, and return changed IDs."""
    rows = (
        list(
            (
                await session.scalars(
                    select(EventParticipant)
                    .join(
                        Event,
                        Event.id == EventParticipant.event_id,
                    )
                    .where(
                        Event.workspace_id == scope.workspace_id,
                        EventParticipant.event_id.in_(event_ids),
                        EventParticipant.entity_id == source_id,
                    )
                    .order_by(EventParticipant.event_id, EventParticipant.id)
                    .with_for_update()
                )
            ).all()
        )
        if event_ids
        else []
    )
    changed_ids: set[UUID] = set()
    for row in rows:
        changed_ids.add(row.event_id)
        duplicate = await session.scalar(
            select(EventParticipant)
            .where(
                EventParticipant.event_id == row.event_id,
                EventParticipant.entity_id == target_id,
                EventParticipant.role == row.role,
            )
            .with_for_update()
        )
        if duplicate is None:
            row.entity_id = target_id
            continue
        evidence = list(
            (
                await session.scalars(
                    select(ParticipantEvidence).where(ParticipantEvidence.participant_id == row.id)
                )
            ).all()
        )
        existing_ids = set(
            (
                await session.scalars(
                    select(ParticipantEvidence.event_evidence_id).where(
                        ParticipantEvidence.participant_id == duplicate.id
                    )
                )
            ).all()
        )
        for support in evidence:
            if support.event_evidence_id in existing_ids:
                await session.delete(support)
            else:
                support.participant_id = duplicate.id
        if row.origin == "manual":
            duplicate.origin = "manual"
        await session.delete(row)
    return sorted(changed_ids, key=str)


async def apply_entity_split(
    session: AsyncSession,
    *,
    source_id: UUID,
    target_id: UUID,
    event_ids: list[UUID],
    selected_pairs: set[tuple[UUID, UUID]],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Move only selected exact-support derived links and return changed event IDs; retain manual links."""
    rows = (
        list(
            (
                await session.scalars(
                    select(EventParticipant)
                    .join(
                        Event,
                        Event.id == EventParticipant.event_id,
                    )
                    .where(
                        Event.workspace_id == scope.workspace_id,
                        EventParticipant.event_id.in_(event_ids),
                        EventParticipant.entity_id == source_id,
                        EventParticipant.origin == "derived",
                    )
                    .order_by(EventParticipant.event_id, EventParticipant.id)
                    .with_for_update()
                )
            ).all()
        )
        if event_ids
        else []
    )
    evidence_by_id = (
        {
            item.id: item
            for item in (
                await session.scalars(
                    select(EventEvidence).where(
                        EventEvidence.workspace_id == scope.workspace_id,
                        EventEvidence.event_id.in_(event_ids),
                    )
                )
            ).all()
        }
        if event_ids
        else {}
    )
    changed_ids: set[UUID] = set()
    for row in rows:
        supports = list(
            (
                await session.scalars(
                    select(ParticipantEvidence).where(ParticipantEvidence.participant_id == row.id)
                )
            ).all()
        )
        selected_supports = [
            item
            for item in supports
            if item.event_evidence_id in evidence_by_id
            and evidence_by_id[item.event_evidence_id].document_version_id is not None
            and evidence_by_id[item.event_evidence_id].chunk_id is not None
            and (
                evidence_by_id[item.event_evidence_id].document_version_id,
                evidence_by_id[item.event_evidence_id].chunk_id,
            )
            in selected_pairs
        ]
        if not selected_supports:
            continue
        changed_ids.add(row.event_id)
        duplicate = await session.scalar(
            select(EventParticipant)
            .where(
                EventParticipant.event_id == row.event_id,
                EventParticipant.entity_id == target_id,
                EventParticipant.role == row.role,
            )
            .with_for_update()
        )
        if duplicate is None:
            duplicate = EventParticipant(
                event_id=row.event_id,
                entity_id=target_id,
                role=row.role,
                metadata_json=row.metadata_json,
                origin="derived",
            )
            session.add(duplicate)
            await session.flush()
        for support in selected_supports:
            support.participant_id = duplicate.id
        if len(selected_supports) == len(supports):
            await session.delete(row)
    return sorted(changed_ids, key=str)


async def remove_entity_participants(
    session: AsyncSession,
    entity_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Remove participant links for terminally deleted entities and return their affected event IDs."""
    if entity_ids:
        event_ids = list(
            (
                await session.scalars(
                    select(EventParticipant.event_id)
                    .join(
                        Event,
                        Event.id == EventParticipant.event_id,
                    )
                    .where(
                        Event.workspace_id == scope.workspace_id,
                        EventParticipant.entity_id.in_(entity_ids),
                    )
                    .distinct()
                )
            ).all()
        )
        await session.execute(
            delete(EventParticipant).where(
                EventParticipant.entity_id.in_(entity_ids),
                EventParticipant.event_id.in_(
                    select(Event.id).where(
                        Event.workspace_id == scope.workspace_id,
                    )
                ),
            )
        )
        return sorted(set(event_ids), key=str)
    return []


async def remove_document_support(
    session: AsyncSession,
    closure: TimelineSupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> list[ReplayDraft]:
    """Remove one document's prepared evidence and only participants whose exact support vanished."""
    if closure.document_id is None:
        raise ValueError("A document closure is required")
    return await _remove_support(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def remove_source_support(
    session: AsyncSession,
    closure: TimelineSupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> list[ReplayDraft]:
    """Remove one Source's prepared support while preserving unrelated evidence and owner fields."""
    if closure.document_id is not None:
        raise ValueError("A Source-wide closure is required")
    return await _remove_support(
        session,
        closure,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
        source_fence=source_fence,
    )


async def _remove_support(
    session: AsyncSession,
    closure: TimelineSupportClosure,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
    access_fence: AccessFence,
    source_fence: SourceFence,
) -> list[ReplayDraft]:
    """Held apply: rows were locked by ``prepare_support_cleanup_in_uow``; flush without commit."""
    _require_cleanup_fences(
        await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled),
        closure=closure,
        scope=scope,
        access_fence=access_fence,
        source_fence=source_fence,
    )
    current = await support_cleanup_ids(
        session,
        source_id=closure.source_id,
        document_id=closure.document_id,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if current.overflow or current != closure:
        raise RuntimeError("cleanup closure changed")
    workspace_id = scope.workspace_id
    if closure.evidence_ids:
        await session.execute(
            delete(EventEvidence).where(
                EventEvidence.workspace_id == workspace_id,
                EventEvidence.id.in_(closure.evidence_ids),
            )
        )
    orphaned = (
        list(
            (
                await session.scalars(
                    select(EventParticipant.id).where(
                        EventParticipant.id.in_(closure.participant_ids),
                        EventParticipant.origin == "derived",
                        EventParticipant.event_id.in_(
                            select(Event.id).where(Event.workspace_id == workspace_id)
                        ),
                        ~EventParticipant.id.in_(select(ParticipantEvidence.participant_id)),
                    )
                )
            ).all()
        )
        if closure.participant_ids
        else []
    )
    if orphaned:
        await session.execute(
            delete(EventParticipant).where(
                EventParticipant.id.in_(orphaned),
                EventParticipant.event_id.in_(
                    select(Event.id).where(Event.workspace_id == workspace_id)
                ),
            )
        )
    for event_id in closure.event_ids:
        event = await session.get(Event, event_id)
        if event is not None and event.workspace_id == workspace_id:
            event.revision += 1
    await _hide_unsupported(session, list(closure.event_ids), workspace_id=workspace_id)
    await session.flush()
    return (
        [make_timeline_collection_change(scope=scope, source_id=closure.source_id)]
        if closure.event_ids
        else []
    )


async def _hide_unsupported(
    session: AsyncSession, event_ids: list[UUID], *, workspace_id: UUID
) -> None:
    """Hide unsupported derived events and scrub every extracted field not explicitly owner-corrected."""
    for event_id in sorted(set(event_ids), key=str):
        event = await session.get(Event, event_id)
        if event is None or event.workspace_id != workspace_id or event.origin != "derived":
            continue
        remaining = await session.scalar(
            select(EventEvidence.id)
            .where(
                EventEvidence.workspace_id == workspace_id,
                EventEvidence.event_id == event_id,
            )
            .limit(1)
        )
        if remaining is None:
            owners = set(event.owner_fields)
            event.title = event.title if "title" in owners else "[unsupported derived event]"
            event.summary = event.summary if "summary" in owners else None
            if not owners:
                event.deleted_at = datetime.now(UTC)
            # Once exact support is gone, retain only fields the owner explicitly
            # corrected; extraction identifiers remain solely as suppression keys.
            event.source_id = None
            if "type" not in owners:
                event.type = "unsupported_derived_event"
            if "subtype" not in owners:
                event.subtype = None
            if "importance_score" not in owners:
                event.importance_score = None
            if "confidence" not in owners:
                event.confidence = None
            if "metadata" not in owners:
                event.metadata_json = {}
            timed_owner = event.date_precision == "timed" and bool(
                owners.intersection({"started_at", "ended_at", "date_precision"})
            )
            date_owner = event.date_precision == "date" and bool(
                owners.intersection({"occurred_date", "end_date", "date_precision"})
            )
            if timed_owner:
                # A start instant is the minimum shape-required anchor when an
                # owner corrected only the end or precision of a timed event.
                event.date_precision = "timed"
                event.occurred_date = event.end_date = None
                event.ended_at = event.ended_at if "ended_at" in owners else None
                event.occurrence_timezone = (
                    event.occurrence_timezone if "occurrence_timezone" in owners else None
                )
            elif date_owner:
                # Keep only the date anchor required to represent an owner date correction.
                event.date_precision = "date"
                event.started_at = event.ended_at = None
                event.end_date = event.end_date if "end_date" in owners else None
                event.occurrence_timezone = (
                    event.occurrence_timezone if "occurrence_timezone" in owners else None
                )
            elif "date_precision" in owners and event.date_precision == "unknown":
                event.date_precision = "unknown"
                event.started_at = event.ended_at = None
                event.occurred_date = event.end_date = None
                event.occurrence_timezone = None
            else:
                event.date_precision = "unknown"
                event.started_at = event.ended_at = None
                event.occurred_date = event.end_date = None
                event.occurrence_timezone = (
                    event.occurrence_timezone if "occurrence_timezone" in owners else None
                )
            if not owners.intersection({"valid_from", "valid_to"}):
                event.valid_from = event.valid_to = None
            elif "valid_to" not in owners:
                event.valid_to = None
            # A corrected exclusive end still needs its current start boundary to satisfy the range shape.
            event.observed_at = event.created_at
            await session.execute(
                delete(EventParticipant).where(
                    EventParticipant.event_id == event_id,
                    EventParticipant.origin == "derived",
                )
            )


async def list_changed_events_after(
    session: AsyncSession,
    position: tuple[datetime, UUID] | None,
    limit: int = 100,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[tuple[datetime, UUID, str, dict[str, Any] | None]]:
    """Read-only cursor page of timeline events by ``(updated_at, id)`` for the automations sweep.

    The key combines event id and revision so an owner revision is a new trigger event. Payload
    carries type, source id and importance only (no title or summary). Soft-deleted events stay in
    the page with a None payload so the cursor advances past them without offering them.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    stmt = select(Event).where(Event.workspace_id == scope.workspace_id)
    if position is not None:
        stmt = stmt.where(tuple_(Event.updated_at, Event.id) > tuple_(*position))
    rows = (await session.scalars(stmt.order_by(Event.updated_at, Event.id).limit(limit))).all()
    result: list[tuple[datetime, UUID, str, dict[str, Any] | None]] = []
    for r in rows:
        if r.deleted_at is not None:
            result.append((r.updated_at, r.id, f"{r.id}:{r.revision}", None))
            continue
        payload: dict[str, Any] = {"event_type": r.type}
        if r.source_id is not None:
            payload["source_id"] = str(r.source_id)
        if r.importance_score is not None:
            payload["importance"] = float(r.importance_score)
        result.append((r.updated_at, r.id, f"{r.id}:{r.revision}", payload))
    return result
