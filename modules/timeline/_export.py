"""Timeline export pages, cursors and fence validation (private to the timeline package)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import b64url_decode, b64url_encode
from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.admission import actor_id as _actor
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.sources import public as sources
from modules.sources.public import SourceExportFence
from modules.timeline._common import TIMELINE_EXPORT_PAGE_MAX_BYTES, _admit
from modules.timeline.models import (
    Event,
    EventEvidence,
    EventParticipant,
    ParticipantEvidence,
)
from modules.timeline.schemas import (
    TimelineExportEvidence,
    TimelineExportFence,
    TimelineExportFenceValidation,
    TimelineExportPage,
    TimelineExportParticipant,
    TimelineExportRead,
)


class _TimelineExportIneligible(Exception):
    """An otherwise persisted derived event has no currently exportable evidence."""


def _encode_timeline_export_cursor(
    owner_id: int,
    workspace_id: UUID,
    snapshot_at: datetime,
    position_at: datetime,
    position_id: UUID,
) -> str:
    """Bind a keyset position to its current owner and immutable snapshot cutoff."""
    value = {
        "v": 1,
        "owner": owner_id,
        "workspace": str(workspace_id),
        "kind": "events",
        "snapshot": snapshot_at.astimezone(UTC).isoformat(),
        "at": position_at.astimezone(UTC).isoformat(),
        "id": str(position_id),
    }
    return b64url_encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _decode_timeline_export_cursor(
    cursor: str, owner_id: int, workspace_id: UUID
) -> tuple[datetime, datetime, UUID]:
    """Reject malformed, noncanonical, future or cross-owner timeline cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        value = json.loads(b64url_decode(cursor))
        if not isinstance(value, dict) or set(value) != {
            "v",
            "owner",
            "workspace",
            "kind",
            "snapshot",
            "at",
            "id",
        }:
            raise ValueError
        if (
            value["v"] != 1
            or value["owner"] != owner_id
            or value["workspace"] != str(workspace_id)
            or value["kind"] != "events"
        ):
            raise ValueError
        snapshot, position = (
            datetime.fromisoformat(value["snapshot"]),
            datetime.fromisoformat(value["at"]),
        )
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot, position)):
            raise ValueError
        snapshot, position = snapshot.astimezone(UTC), position.astimezone(UTC)
        row_id = UUID(value["id"])
        if (
            snapshot > datetime.now(UTC)
            or _encode_timeline_export_cursor(owner_id, workspace_id, snapshot, position, row_id)
            != cursor
        ):
            raise ValueError
        return snapshot, position, row_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid timeline export cursor") from exc


def _timeline_export_json(value: object) -> bytes:
    """Serialize detached DTO data as canonical compact UTF-8 JSON for size and digest fences."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _timeline_export_statement(snapshot_at: datetime, *, scope: Scope) -> Select[Event]:
    """Select undeleted workspace events and derived events with retained eligible evidence."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    support = exists(
        select(EventEvidence.id).where(
            EventEvidence.workspace_id == scope.workspace_id,
            EventEvidence.event_id == Event.id,
            EventEvidence.source_id.in_(eligible),
            EventEvidence.document_version_id.is_not(None),
            EventEvidence.chunk_id.is_not(None),
        )
    )
    return select(Event).where(
        Event.workspace_id == scope.workspace_id,
        Event.deleted_at.is_(None),
        Event.created_at <= snapshot_at,
        Event.updated_at <= snapshot_at,
        or_(Event.origin == "manual", support),
    )


async def _timeline_export_count(
    session: AsyncSession,
    snapshot_at: datetime,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> int:
    """Count only cutoff-stable owner facts or derived events with exact retained support."""
    count, position = 0, None
    base = _timeline_export_statement(snapshot_at, scope=scope)
    while True:
        statement = base
        if position is not None:
            statement = statement.where(tuple_(Event.created_at, Event.id) > position)
        rows = list(
            (
                await session.scalars(
                    statement.order_by(Event.created_at, Event.id)
                    .limit(128)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not rows:
            return count
        for event in rows:
            try:
                await _timeline_export_record(
                    session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
                )
                count += 1
            except _TimelineExportIneligible:
                continue
        position = (rows[-1].created_at, rows[-1].id)


async def _export_participant_rows(
    session: AsyncSession, event: Event, *, scope: Scope
) -> list[EventParticipant]:
    """Read an event's participants in stable role/entity order, bounded at 100.

    Fetches 101 rows with ``populate_existing`` so stale identity-map copies never leak into the
    export; raises ``ValueError`` when the bound is exceeded rather than truncating silently.
    """
    participants = list(
        (
            await session.scalars(
                select(EventParticipant)
                .join(
                    Event,
                    Event.id == EventParticipant.event_id,
                )
                .where(
                    Event.workspace_id == scope.workspace_id,
                    EventParticipant.event_id == event.id,
                )
                .order_by(EventParticipant.role, EventParticipant.entity_id)
                .limit(101)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(participants) > 100:
        raise ValueError("An event export record exceeds the participant bound")
    return participants


async def _export_evidence_rows(
    session: AsyncSession, event: Event, *, scope: Scope
) -> list[EventEvidence]:
    """Read an event's export-eligible-source evidence rows with full document/chunk refs, bounded at 100.

    Fetches 101 rows with ``populate_existing``; raises ``ValueError`` over the bound. Document
    liveness is proven per row later by the documents owner; this only narrows candidates.
    """
    evidence_rows = list(
        (
            await session.scalars(
                select(EventEvidence)
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                    EventEvidence.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
                    EventEvidence.document_version_id.is_not(None),
                    EventEvidence.chunk_id.is_not(None),
                )
                .order_by(EventEvidence.id)
                .limit(101)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(evidence_rows) > 100:
        raise ValueError("An event export record exceeds the evidence bound")
    return evidence_rows


def _export_read(
    event: Event,
    participant_refs: list[TimelineExportParticipant],
    validated: list[TimelineExportEvidence],
) -> TimelineExportRead:
    """Project an event and its already-validated children into the export DTO.

    Copies only the enumerated scalar fields; arbitrary event metadata is never exported. Pure.
    """
    return TimelineExportRead(
        id=event.id,
        type=event.type,
        subtype=event.subtype,
        title=event.title,
        summary=event.summary,
        importance_score=event.importance_score,
        confidence=event.confidence,
        origin=event.origin,
        date_precision=event.date_precision,
        started_at=event.started_at,
        ended_at=event.ended_at,
        occurred_date=event.occurred_date,
        end_date=event.end_date,
        occurrence_timezone=event.occurrence_timezone,
        observed_at=event.observed_at,
        valid_from=event.valid_from,
        valid_to=event.valid_to,
        revision=event.revision,
        created_at=event.created_at,
        updated_at=event.updated_at,
        participants=participant_refs,
        evidence=validated,
    )


async def _timeline_export_record(
    session: AsyncSession,
    event: Event,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[TimelineExportRead, str, str, list[tuple[UUID, int]]]:
    """Project one fresh event and its exact supported children without arbitrary metadata."""
    participants = await _export_participant_rows(session, event, scope=scope)
    evidence_rows = await _export_evidence_rows(session, event, scope=scope)
    validated: list[TimelineExportEvidence] = []
    valid_evidence_ids: set[UUID] = set()
    current_source_fences: set[tuple[UUID, int]] = set()
    for evidence_row in evidence_rows:
        if (
            evidence_row.source_id is None
            or evidence_row.source_generation is None
            or evidence_row.document_id is None
            or evidence_row.document_version_id is None
            or evidence_row.chunk_id is None
        ):
            continue
        proof = await documents.export_timeline_evidence(
            session,
            documents.TimelineExportEvidenceCandidate(
                evidence_id=evidence_row.id,
                source_id=evidence_row.source_id,
                accepted_source_generation=evidence_row.source_generation,
                document_id=evidence_row.document_id,
                document_version_id=evidence_row.document_version_id,
                chunk_id=evidence_row.chunk_id,
            ),
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if proof is None:
            continue
        valid_evidence_ids.add(evidence_row.id)
        current_source_fences.add((proof.source_id, proof.current_source_generation))
        validated.append(
            TimelineExportEvidence(
                source_id=proof.source_id,
                source_generation=proof.accepted_source_generation,
                document_id=proof.document_id,
                document_version_id=proof.document_version_id,
                chunk_id=proof.chunk_id,
            )
        )
    if len(validated) > 100:
        raise ValueError("An event export record exceeds the evidence bound")
    if event.origin == "derived" and not validated:
        raise _TimelineExportIneligible("A derived event has no currently exportable evidence")
    supported_participant_ids = (
        set(
            await session.scalars(
                select(ParticipantEvidence.participant_id)
                .where(
                    ParticipantEvidence.event_evidence_id.in_(valid_evidence_ids),
                )
                .execution_options(populate_existing=True)
            )
        )
        if valid_evidence_ids
        else set()
    )
    participant_refs = [
        TimelineExportParticipant(entity_id=item.entity_id, role=item.role, origin=item.origin)
        for item in participants
        if item.origin == "manual" or item.id in supported_participant_ids
    ]
    item = _export_read(event, participant_refs, validated)
    participant_digest = hashlib.sha256(
        _timeline_export_json([ref.model_dump(mode="json") for ref in participant_refs])
    ).hexdigest()
    evidence_digest = hashlib.sha256(
        _timeline_export_json([ref.model_dump(mode="json") for ref in validated])
    ).hexdigest()
    source_fences = sorted(current_source_fences, key=lambda pair: str(pair[0]))
    return item, participant_digest, evidence_digest, source_fences


async def export_page(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelineExportPage:
    """Return a bounded fixed-cutoff event page with independently retained owner facts and citations."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "events" or not 1 <= limit <= 100:
        raise ValueError("Timeline export kind or limit is invalid")
    if owner_id != _actor(scope):
        raise PermissionError("Timeline export requires the workspace owner")
    if cursor is None:
        snapshot, position = datetime.now(UTC), None
    else:
        snapshot, position_at, position_id = _decode_timeline_export_cursor(
            cursor, owner_id, scope.workspace_id
        )
        position = (position_at, position_id)
    count = await _timeline_export_count(
        session, snapshot, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    statement = _timeline_export_statement(snapshot, scope=scope)
    if position is not None:
        statement = statement.where(tuple_(Event.created_at, Event.id) > position)
    rows = list(
        (
            await session.scalars(
                statement.order_by(Event.created_at, Event.id)
                .limit(limit + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    more = len(rows) > limit
    items: list[TimelineExportRead] = []
    fences: list[TimelineExportFence] = []
    last_examined: tuple[datetime, UUID] | None = None
    for event in rows[:limit]:
        try:
            (
                item,
                participant_digest,
                evidence_digest,
                source_fences,
            ) = await _timeline_export_record(
                session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        except _TimelineExportIneligible:
            last_examined = (event.created_at, event.id)
            continue
        size = len(
            _timeline_export_json([value.model_dump(mode="json") for value in items + [item]])
        )
        if size > TIMELINE_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An event export record exceeds the page byte budget")
            more = True
            break
        items.append(item)
        last_examined = (event.created_at, event.id)
        fences.append(
            TimelineExportFence(
                id=event.id,
                created_at=event.created_at,
                updated_at=event.updated_at,
                revision=event.revision,
                record_digest=hashlib.sha256(
                    _timeline_export_json(item.model_dump(mode="json"))
                ).hexdigest(),
                evidence_digest=evidence_digest,
                participant_digest=participant_digest,
                source_fences=source_fences,
            )
        )
    next_cursor = (
        _encode_timeline_export_cursor(owner_id, scope.workspace_id, snapshot, *last_examined)
        if more and last_examined is not None
        else None
    )
    payload_bytes = len(_timeline_export_json([item.model_dump(mode="json") for item in items]))
    return TimelineExportPage(
        owner_id=owner_id,
        record_kind="events",
        snapshot_at=snapshot,
        snapshot_count=count,
        items=items,
        fences=fences,
        payload_bytes=payload_bytes,
        next_cursor=next_cursor,
    )


async def validate_export_fences(
    session: AsyncSession,
    *,
    owner_id: int,
    record_kind: str,
    snapshot_at: datetime,
    expected_snapshot_count: int,
    fences: list[TimelineExportFence],
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelineExportFenceValidation:
    """Re-read every event and child projection and reject source, revision or count drift."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "events" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Timeline export validation input is invalid")
    if owner_id != _actor(scope):
        return TimelineExportFenceValidation(
            valid=False, reason="owner_unavailable", observed_snapshot_count=0
        )
    observed = await _timeline_export_count(
        session, snapshot_at, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if observed != expected_snapshot_count:
        return TimelineExportFenceValidation(
            valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed
        )
    for fence in fences:
        event = await session.scalar(
            select(Event)
            .where(
                Event.workspace_id == scope.workspace_id,
                Event.id == fence.id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            event is None
            or event.deleted_at is not None
            or (event.created_at, event.updated_at, event.revision)
            != (
                fence.created_at,
                fence.updated_at,
                fence.revision,
            )
        ):
            return TimelineExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        try:
            (
                item,
                participant_digest,
                evidence_digest,
                source_fences,
            ) = await _timeline_export_record(
                session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
        except _TimelineExportIneligible:
            return TimelineExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        record_digest = hashlib.sha256(
            _timeline_export_json(item.model_dump(mode="json"))
        ).hexdigest()
        if (
            record_digest != fence.record_digest
            or participant_digest != fence.participant_digest
            or evidence_digest != fence.evidence_digest
            or source_fences != fence.source_fences
        ):
            return TimelineExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
        retained_sources = await sources.filter_export_eligible_sources(
            session,
            [
                SourceExportFence(
                    source_id=source_id, workspace_id=scope.workspace_id, generation=generation
                )
                for source_id, generation in fence.source_fences
            ],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if set(retained_sources) != {source_id for source_id, _ in fence.source_fences}:
            return TimelineExportFenceValidation(
                valid=False, reason="record_changed", observed_snapshot_count=observed
            )
    return TimelineExportFenceValidation(
        valid=True, reason="valid", observed_snapshot_count=observed
    )
