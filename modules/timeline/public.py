"""Public detached event queries, owner commands, and cleanup contracts."""

import base64
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Select, delete, desc, exists, false, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.realtime import (
    ReplayDraft,
    commit_with_replay,
    make_timeline_change,
    make_timeline_collection_change,
)
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.sources import public as sources
from modules.sources.schemas import SourceExportFence
from modules.timeline.models import (
    Event,
    EventAudit,
    EventEvidence,
    EventParticipant,
    EventSuppression,
    ParticipantEvidence,
)
from modules.timeline.schemas import (
    BriefEventSupport,
    CorrelationSignalPage,
    CorrelationSignalRead,
    EventCreate,
    EventPage,
    EventPatch,
    EventRead,
    TimelineExportEvidence,
    TimelineExportFence,
    TimelineExportFenceValidation,
    TimelineExportPage,
    TimelineExportParticipant,
    TimelineExportRead,
    TimelinePage,
    TimelineQuery,
)


def _actor(scope: Scope) -> int:
    """Return the principal recorded by a real workspace or durable job scope."""
    return scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id


async def _admit(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
    lock: bool = False, expected: AccessFence | None = None,
) -> AccessFence:
    """Require owner scope and capture or lock authorization before event locks."""
    if not isinstance(scope, (WorkspaceContext, InternalJobScope)):
        raise TypeError("An explicit timeline workspace scope is required")
    if isinstance(scope, WorkspaceContext) and scope.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    if type(multi_workspace_enabled) is not bool:
        raise TypeError("The configured multi-workspace feature flag must be a boolean")
    if lock:
        return await workspaces.lock_access_fence(
            session, scope=scope, expected=expected, multi_workspace_enabled=multi_workspace_enabled,
        )
    return await workspaces.read_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
from modules.timeline.seed import (
    ensure_demo_events,  # re-export: used by documents seed
)

# Explicit re-exports consumed by other modules (mypy strict forbids implicit re-export).
__all__ = [
    "ensure_demo_events",
]

MAX_PAGE = 100
TIMELINE_EXPORT_PAGE_MAX_BYTES = 16_777_216


class _TimelineExportIneligible(Exception):
    """An otherwise persisted derived event has no currently exportable evidence."""


def _encode_timeline_export_cursor(owner_id: int, snapshot_at: datetime, position_at: datetime, position_id: UUID) -> str:
    """Bind a keyset position to its current owner and immutable snapshot cutoff."""
    value = {"v": 1, "owner": owner_id, "kind": "events", "snapshot": snapshot_at.astimezone(UTC).isoformat(),
             "at": position_at.astimezone(UTC).isoformat(), "id": str(position_id)}
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_timeline_export_cursor(cursor: str, owner_id: int) -> tuple[datetime, datetime, UUID]:
    """Reject malformed, noncanonical, future or cross-owner timeline cursors."""
    try:
        if not cursor or len(cursor) > 1024 or "=" in cursor:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"v", "owner", "kind", "snapshot", "at", "id"}:
            raise ValueError
        if value["v"] != 1 or value["owner"] != owner_id or value["kind"] != "events":
            raise ValueError
        snapshot, position = datetime.fromisoformat(value["snapshot"]), datetime.fromisoformat(value["at"])
        if any(item.tzinfo is None or item.utcoffset() is None for item in (snapshot, position)):
            raise ValueError
        snapshot, position = snapshot.astimezone(UTC), position.astimezone(UTC)
        row_id = UUID(value["id"])
        if snapshot > datetime.now(UTC) or _encode_timeline_export_cursor(owner_id, snapshot, position, row_id) != cursor:
            raise ValueError
        return snapshot, position, row_id
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid timeline export cursor") from exc


def _timeline_export_json(value: object) -> bytes:
    """Serialize detached DTO data as canonical compact UTF-8 JSON for size and digest fences."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _timeline_export_statement(snapshot_at: datetime, *, scope: Scope) -> Select[Event]:
    """Select undeleted workspace events and derived events with retained eligible evidence."""
    eligible = sources.export_eligible_source_ids(scope=scope)
    support = exists(select(EventEvidence.id).where(
        EventEvidence.workspace_id == scope.workspace_id,
        EventEvidence.event_id == Event.id, EventEvidence.source_id.in_(eligible),
        EventEvidence.document_version_id.is_not(None), EventEvidence.chunk_id.is_not(None),
    ))
    return select(Event).where(
        Event.workspace_id == scope.workspace_id,
        Event.deleted_at.is_(None), Event.created_at <= snapshot_at, Event.updated_at <= snapshot_at,
        or_(Event.origin == "manual", support),
    )


async def _timeline_export_count(
    session: AsyncSession, snapshot_at: datetime, *, scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Count only cutoff-stable owner facts or derived events with exact retained support."""
    count, position = 0, None
    base = _timeline_export_statement(snapshot_at, scope=scope)
    while True:
        statement = base
        if position is not None:
            statement = statement.where(tuple_(Event.created_at, Event.id) > position)
        rows = list((await session.scalars(statement.order_by(Event.created_at, Event.id).limit(128)
                                           .execution_options(populate_existing=True))).all())
        if not rows:
            return count
        for event in rows:
            try:
                await _timeline_export_record(
                    session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
                count += 1
            except _TimelineExportIneligible:
                continue
        position = (rows[-1].created_at, rows[-1].id)


async def _timeline_export_record(
    session: AsyncSession, event: Event, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[TimelineExportRead, str, str, list[tuple[UUID, int]]]:
    """Project one fresh event and its exact supported children without arbitrary metadata."""
    participants = list((await session.scalars(select(EventParticipant).join(
        Event, Event.id == EventParticipant.event_id,
    ).where(
        Event.workspace_id == scope.workspace_id, EventParticipant.event_id == event.id,
    ).order_by(EventParticipant.role, EventParticipant.entity_id).limit(101)
      .execution_options(populate_existing=True))).all())
    if len(participants) > 100:
        raise ValueError("An event export record exceeds the participant bound")
    evidence_rows = list((await session.scalars(select(EventEvidence).where(
        EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
        EventEvidence.source_id.in_(sources.export_eligible_source_ids(scope=scope)),
        EventEvidence.document_version_id.is_not(None), EventEvidence.chunk_id.is_not(None),
    ).order_by(EventEvidence.id).limit(101).execution_options(populate_existing=True))).all())
    if len(evidence_rows) > 100:
        raise ValueError("An event export record exceeds the evidence bound")
    validated: list[TimelineExportEvidence] = []
    valid_evidence_ids: set[UUID] = set()
    current_source_fences: set[tuple[UUID, int]] = set()
    for evidence_row in evidence_rows:
        if (evidence_row.source_id is None or evidence_row.source_generation is None
                or evidence_row.document_id is None or evidence_row.document_version_id is None
                or evidence_row.chunk_id is None):
            continue
        proof = await documents.export_timeline_evidence(session, documents.TimelineExportEvidenceCandidate(
            evidence_id=evidence_row.id, source_id=evidence_row.source_id,
            accepted_source_generation=evidence_row.source_generation,
            document_id=evidence_row.document_id, document_version_id=evidence_row.document_version_id,
            chunk_id=evidence_row.chunk_id,
        ), scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if proof is None:
            continue
        valid_evidence_ids.add(evidence_row.id)
        current_source_fences.add((proof.source_id, proof.current_source_generation))
        validated.append(TimelineExportEvidence(
            source_id=proof.source_id, source_generation=proof.accepted_source_generation,
            document_id=proof.document_id, document_version_id=proof.document_version_id,
            chunk_id=proof.chunk_id,
        ))
    if len(validated) > 100:
        raise ValueError("An event export record exceeds the evidence bound")
    if event.origin == "derived" and not validated:
        raise _TimelineExportIneligible("A derived event has no currently exportable evidence")
    supported_participant_ids = set(await session.scalars(select(ParticipantEvidence.participant_id).where(
        ParticipantEvidence.event_evidence_id.in_(valid_evidence_ids),
    ).execution_options(populate_existing=True))) if valid_evidence_ids else set()
    participant_refs = [TimelineExportParticipant(entity_id=item.entity_id, role=item.role, origin=item.origin)
                        for item in participants
                        if item.origin == "manual" or item.id in supported_participant_ids]
    item = TimelineExportRead(
        id=event.id, type=event.type, subtype=event.subtype, title=event.title, summary=event.summary,
        importance_score=event.importance_score, confidence=event.confidence,
        origin=event.origin, date_precision=event.date_precision, started_at=event.started_at,
        ended_at=event.ended_at, occurred_date=event.occurred_date, end_date=event.end_date,
        occurrence_timezone=event.occurrence_timezone, observed_at=event.observed_at,
        valid_from=event.valid_from, valid_to=event.valid_to, revision=event.revision,
        created_at=event.created_at, updated_at=event.updated_at,
        participants=participant_refs, evidence=validated,
    )
    participant_digest = hashlib.sha256(_timeline_export_json([ref.model_dump(mode="json") for ref in participant_refs])).hexdigest()
    evidence_digest = hashlib.sha256(_timeline_export_json([ref.model_dump(mode="json") for ref in validated])).hexdigest()
    source_fences = sorted(current_source_fences, key=lambda pair: str(pair[0]))
    return item, participant_digest, evidence_digest, source_fences


async def export_page(session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50,
                      cursor: str | None = None, scope: Scope,
                      multi_workspace_enabled: bool) -> TimelineExportPage:
    """Return a bounded fixed-cutoff event page with independently retained owner facts and citations."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "events" or not 1 <= limit <= 100:
        raise ValueError("Timeline export kind or limit is invalid")
    if owner_id != _actor(scope):
        raise PermissionError("Timeline export requires the workspace owner")
    if cursor is None:
        snapshot, position = datetime.now(UTC), None
    else:
        snapshot, position_at, position_id = _decode_timeline_export_cursor(cursor, owner_id)
        position = (position_at, position_id)
    count = await _timeline_export_count(
        session, snapshot, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = _timeline_export_statement(snapshot, scope=scope)
    if position is not None:
        statement = statement.where(tuple_(Event.created_at, Event.id) > position)
    rows = list((await session.scalars(statement.order_by(Event.created_at, Event.id).limit(limit + 1)
                                       .execution_options(populate_existing=True))).all())
    more = len(rows) > limit
    items: list[TimelineExportRead] = []
    fences: list[TimelineExportFence] = []
    last_examined: tuple[datetime, UUID] | None = None
    for event in rows[:limit]:
        try:
            item, participant_digest, evidence_digest, source_fences = await _timeline_export_record(
                session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        except _TimelineExportIneligible:
            last_examined = (event.created_at, event.id)
            continue
        size = len(_timeline_export_json([value.model_dump(mode="json") for value in items + [item]]))
        if size > TIMELINE_EXPORT_PAGE_MAX_BYTES:
            if not items:
                raise ValueError("An event export record exceeds the page byte budget")
            more = True
            break
        items.append(item)
        last_examined = (event.created_at, event.id)
        fences.append(TimelineExportFence(
            id=event.id, created_at=event.created_at, updated_at=event.updated_at, revision=event.revision,
            record_digest=hashlib.sha256(_timeline_export_json(item.model_dump(mode="json"))).hexdigest(),
            evidence_digest=evidence_digest, participant_digest=participant_digest, source_fences=source_fences,
        ))
    next_cursor = (_encode_timeline_export_cursor(owner_id, snapshot, *last_examined)
                   if more and last_examined is not None else None)
    payload_bytes = len(_timeline_export_json([item.model_dump(mode="json") for item in items]))
    return TimelineExportPage(owner_id=owner_id, record_kind="events", snapshot_at=snapshot,
                              snapshot_count=count, items=items, fences=fences, payload_bytes=payload_bytes,
                              next_cursor=next_cursor)


async def validate_export_fences(session: AsyncSession, *, owner_id: int, record_kind: str,
                                 snapshot_at: datetime, expected_snapshot_count: int,
                                 fences: list[TimelineExportFence], scope: Scope,
                                 multi_workspace_enabled: bool) -> TimelineExportFenceValidation:
    """Re-read every event and child projection and reject source, revision or count drift."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if record_kind != "events" or len(fences) > 100 or expected_snapshot_count < 0:
        raise ValueError("Timeline export validation input is invalid")
    if owner_id != _actor(scope):
        return TimelineExportFenceValidation(valid=False, reason="owner_unavailable", observed_snapshot_count=0)
    observed = await _timeline_export_count(
        session, snapshot_at, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if observed != expected_snapshot_count:
        return TimelineExportFenceValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        event = await session.scalar(select(Event).where(
            Event.workspace_id == scope.workspace_id, Event.id == fence.id,
        ).execution_options(populate_existing=True))
        if event is None or event.deleted_at is not None or (event.created_at, event.updated_at, event.revision) != (
            fence.created_at, fence.updated_at, fence.revision,
        ):
            return TimelineExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        try:
            item, participant_digest, evidence_digest, source_fences = await _timeline_export_record(
                session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        except _TimelineExportIneligible:
            return TimelineExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        record_digest = hashlib.sha256(_timeline_export_json(item.model_dump(mode="json"))).hexdigest()
        if (record_digest != fence.record_digest or participant_digest != fence.participant_digest
                or evidence_digest != fence.evidence_digest
                or source_fences != fence.source_fences):
            return TimelineExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        retained_sources = await sources.filter_export_eligible_sources(session, [
            SourceExportFence(source_id=source_id, workspace_id=scope.workspace_id, generation=generation)
            for source_id, generation in fence.source_fences
        ], scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if set(retained_sources) != {source_id for source_id, _ in fence.source_fences}:
            return TimelineExportFenceValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return TimelineExportFenceValidation(valid=True, reason="valid", observed_snapshot_count=observed)


def day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Return local calendar-day boundaries as UTC using independent zone conversions.

    Folded local midnight chooses its earliest valid instant. A skipped or
    nonexistent midnight raises ValueError rather than shifting the boundary.
    """
    zone = ZoneInfo(timezone)

    def boundary(value: date) -> datetime:
        """Resolve a local midnight and verify that the timezone round-trip preserves it."""
        naive = datetime.combine(value, time.min)
        candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        valid = [item for item in candidates if item.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive]
        if not valid:
            raise ValueError("calendar boundary is nonexistent in the requested timezone")
        return min(valid, key=lambda item: item.astimezone(UTC)).astimezone(UTC)

    return boundary(day), boundary(day + timedelta(days=1))


def _cursor_encode(value: dict[str, object]) -> str:
    """Encode bounded canonical JSON into an opaque URL-safe cursor."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(value: str | None, fingerprint: str) -> dict[str, Any]:
    """Validate cursor version and normalized-filter fingerprint before paging."""
    if not value:
        return {"v": 1, "f": fingerprint, "p": 0, "k": None}
    if len(value) > 1024:
        raise ValueError("cursor is too long")
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("f") != fingerprint:
        raise ValueError("cursor does not match this query")
    if type(payload.get("p")) is not int or payload["p"] not in range(3):
        raise ValueError("cursor partition is invalid")
    key = payload.get("k")
    if key is not None and (
        not isinstance(key, list) or len(key) != 2
        or not all(isinstance(item, str) for item in key)
    ):
        raise ValueError("cursor key is malformed")
    if key is not None:
        try:
            if str(UUID(key[1])) != key[1]:
                raise ValueError("cursor ID is not canonical")
            if payload["p"] == 1:
                parsed_date = date.fromisoformat(key[0])
                if parsed_date.isoformat() != key[0]:
                    raise ValueError("cursor date is not canonical")
            else:
                parsed_instant = datetime.fromisoformat(key[0])
                if parsed_instant.tzinfo is None or parsed_instant.utcoffset() is None:
                    raise ValueError("cursor timestamp must be aware")
                assert parsed_instant is not None
                if parsed_instant.utcoffset() != timedelta(0) or parsed_instant.astimezone(UTC).isoformat() != key[0]:
                    raise ValueError("cursor timestamp is not canonical UTC")
        except (ValueError, TypeError) as exc:
            raise ValueError("cursor key is malformed") from exc
    return payload


async def _event_read(
    session: AsyncSession, event: Event, *, scope: Scope, multi_workspace_enabled: bool,
) -> EventRead:
    """Build an owner DTO from canonical rows and currently valid exact evidence."""
    participants = list((await session.scalars(
        select(EventParticipant).join(Event, Event.id == EventParticipant.event_id).where(
            Event.workspace_id == scope.workspace_id, EventParticipant.event_id == event.id,
        ).order_by(EventParticipant.role, EventParticipant.entity_id)
    )).all())
    evidence_rows = list((await session.scalars(
        select(EventEvidence).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
        ).order_by(EventEvidence.id)
    )).all())
    evidence: list[dict[str, Any]] = []
    for item in evidence_rows:
        if item.document_version_id is None or item.chunk_id is None:
            continue
        refs = await documents.read_evidence_refs(
            session, [(item.document_version_id, item.chunk_id)], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if not refs:
            continue
        ref = refs[0]
        evidence.append({
            "source_id": ref.source_id, "document_id": ref.document_id,
            "document_version_id": ref.document_version_id, "version_number": ref.version_number,
            "chunk_id": ref.chunk_id, "observed_at": ref.observed_at,
            "title": item.title_snapshot if item.metadata_is_version_snapshot else ref.title,
            "canonical_url": item.url_snapshot if item.metadata_is_version_snapshot else ref.canonical_url,
            "metadata_is_version_snapshot": item.metadata_is_version_snapshot,
            "excerpt": ref.excerpt,
        })
    return EventRead(
        id=event.id, source_id=event.source_id, type=event.type, subtype=event.subtype,
        title=event.title, summary=event.summary, importance_score=event.importance_score,
        confidence=event.confidence, metadata=event.metadata_json, origin=event.origin,
        date_precision=event.date_precision, started_at=event.started_at, ended_at=event.ended_at,
        occurred_date=event.occurred_date, end_date=event.end_date,
        occurrence_timezone=event.occurrence_timezone, observed_at=event.observed_at,
        valid_from=event.valid_from, valid_to=event.valid_to, revision=event.revision,
        created_at=event.created_at, updated_at=event.updated_at,
        participants=[{"entity_id": item.entity_id, "role": item.role, "metadata": item.metadata_json} for item in participants],
        evidence=evidence,
    )


async def get_event(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> EventRead | None:
    """Read one visible event with detached participant and evidence projections."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(Event).where(
        Event.id == event_id, Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None),
    ))
    if event is None:
        return None
    if event.origin == "derived" and await session.scalar(select(EventEvidence.id).where(
        EventEvidence.workspace_id == scope.workspace_id,
        EventEvidence.event_id == event.id, EventEvidence.document_version_id.is_not(None),
        EventEvidence.chunk_id.is_not(None),
    ).limit(1)) is None:
        return None
    return await _event_read(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def list_event_evidence(
    session: AsyncSession, event_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[dict[str, Any]] | None:
    """Return exact live evidence references for a visible event, or None when absent."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(select(Event.id).where(
        Event.id == event_id, Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None),
    ))
    if event is None:
        return None
    event_row = await session.scalar(select(Event).where(
        Event.id == event_id, Event.workspace_id == scope.workspace_id,
    ))
    assert event_row is not None  # id was just selected above
    result = await _event_read(session, event_row, scope=scope,
                               multi_workspace_enabled=multi_workspace_enabled)
    return result.evidence if result else None


async def lock_brief_events(
    session: AsyncSession, event_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> set[UUID]:
    """Share-lock a bounded event set in canonical UUID order for Dashboard capture.

    Missing or deleted events are omitted; subsequent support reads decide
    whether each fact remains eligible. Sorted acquisition avoids deadlocks when
    a prompt contains several timeline facts.
    """
    ids = sorted(set(event_ids), key=str)
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(ids) > 40:
        raise ValueError("Dashboard brief event lock set exceeds its fact limit")
    if not ids:
        return set()
    locked = await session.scalars(
        select(Event.id).where(
            Event.workspace_id == scope.workspace_id, Event.id.in_(ids), Event.deleted_at.is_(None),
        )
        .order_by(Event.id).with_for_update(read=True)
    )
    return set(locked)


async def brief_event_support(
    session: AsyncSession, event_id: UUID, *, expected_title: str | None,
    expected_source_ids: list[str], lock_fact: bool = False, scope: Scope, multi_workspace_enabled: bool,
) -> BriefEventSupport:
    """Return complete exact evidence or actual evidence-free manual origin for a brief fact.

    At most 100 support references are returned. The caller may request a shared
    event-row lock after it has acquired canonical source and document locks, so
    owner edits cannot race Dashboard model egress or final persistence.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(Event).where(
        Event.id == event_id, Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None),
    )
    if lock_fact:
        statement = statement.with_for_update(read=True, of=Event)
    event = await session.scalar(statement.execution_options(populate_existing=True))
    unavailable = BriefEventSupport(
        event_id=event_id, title=expected_title or "", event_type="", origin="manual",
        source_ids=[], evidence=[], complete=False, independent=False,
    )
    if event is None or (expected_title is not None and event.title != expected_title):
        return unavailable
    evidence_rows = list((await session.scalars(select(EventEvidence).where(
        EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
    ).order_by(EventEvidence.id).limit(101))).all())
    if not evidence_rows:
        independent = event.origin == "manual" and not expected_source_ids
        return BriefEventSupport(
            event_id=event.id, title=event.title, event_type=event.type, origin=event.origin,
            source_ids=[], evidence=[], complete=independent, independent=independent,
        )
    if len(evidence_rows) > 100 or any(
        row.document_version_id is None or row.chunk_id is None for row in evidence_rows
    ):
        return unavailable.model_copy(update={"origin": event.origin})
    pairs = [(row.document_version_id, row.chunk_id) for row in evidence_rows
             if row.document_version_id is not None and row.chunk_id is not None]  # None rows returned above
    refs = await documents.read_evidence_refs(
        session, pairs, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(refs) != len(pairs):
        return unavailable.model_copy(update={"origin": event.origin})
    support = [
        {"document_id": ref.document_id, "document_version_id": ref.document_version_id,
         "chunk_id": ref.chunk_id, "source_id": ref.source_id}
        for ref in refs
    ]
    source_ids = sorted({ref["source_id"] for ref in support}, key=str)
    expected = sorted(set(expected_source_ids))
    if ([str(value) for value in source_ids] != expected
            or len({(ref["document_id"], ref["document_version_id"], ref["chunk_id"]) for ref in support}) != len(support)):
        return unavailable.model_copy(update={"origin": event.origin})
    return BriefEventSupport(
        event_id=event.id, title=event.title, event_type=event.type, origin=event.origin,
        source_ids=source_ids, evidence=support, complete=True, independent=False,
    )


async def list_correlation_signals(
    session: AsyncSession, *, domain: str, from_at: datetime, to_at: datetime,
    regions: list[str], source_ids: list[UUID], limit: int = 100, scope: Scope,
    multi_workspace_enabled: bool,
) -> CorrelationSignalPage:
    """Project a bounded timed event slice into detached IDs after exact evidence checks.

    Region values must already be recorded under event metadata ``region``. Events
    without a live document-version/chunk reference are excluded. Military and
    escalation labels also require a recorded participant entity. No event title,
    summary, excerpt, or coordinate is returned to the correlation owner.
    """
    if (
        from_at.tzinfo is None or from_at.utcoffset() is None
        or to_at.tzinfo is None or to_at.utcoffset() is None
        or domain not in {"military", "economic", "disaster", "escalation"}
        or from_at >= to_at or not 1 <= limit <= 100
        or not 1 <= len(regions) <= 32 or len(regions) != len(set(regions))
        or not 1 <= len(source_ids) <= 32 or len(source_ids) != len(set(source_ids))
        or any(not region or len(region) > 80 or region != region.strip() for region in regions)
    ):
        raise ValueError("Invalid bounded correlation projection scope")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    selected_sources = set(source_ids)
    start, end = from_at.astimezone(UTC), to_at.astimezone(UTC)
    statement = select(Event).where(
        Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None), Event.date_precision == "timed",
        Event.started_at >= start, Event.started_at < end,
        Event.type == domain,
        Event.metadata_json["region"].as_string().in_(regions),
        Event.id.in_(select(EventEvidence.event_id).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == Event.id,
            EventEvidence.document_version_id.is_not(None),
            EventEvidence.chunk_id.is_not(None),
        )),
        # Coarse persisted-source admission bounds candidate work; live Document refs below
        # make the final decision and prevent stale EventEvidence.source_id from authorizing IDs.
        or_(
            Event.source_id.in_(selected_sources),
            Event.id.in_(select(EventEvidence.event_id).where(
                EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == Event.id,
                EventEvidence.source_id.in_(selected_sources),
                EventEvidence.document_version_id.is_not(None),
                EventEvidence.chunk_id.is_not(None),
            )),
        ),
    ).order_by(Event.started_at, Event.id).limit(1_001)
    rows = list((await session.scalars(statement)).all())
    signals: list[CorrelationSignalRead] = []
    for event in rows:
        if event.source_id is not None and event.source_id not in selected_sources:
            continue
        current = await _event_read(session, event, scope=scope,
                                    multi_workspace_enabled=multi_workspace_enabled)
        if not current.evidence or (
            event.type in {"military", "escalation"} and not current.participants
        ):
            continue
        live_evidence = {
            (item["document_version_id"], item["chunk_id"]): item
            for item in current.evidence if item["source_id"] in selected_sources
        }
        evidence_rows = (await session.scalars(select(EventEvidence).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
        ).order_by(EventEvidence.id))).all()
        exact_rows = [item for item in evidence_rows if item.source_id in selected_sources and (
            item.document_version_id, item.chunk_id
        ) in live_evidence]
        if not exact_rows:
            continue
        exact_refs = [live_evidence[(item.document_version_id, item.chunk_id)] for item in exact_rows]
        event_evidence_ids = [item.id for item in exact_rows]
        document_ids = sorted({item["document_id"] for item in exact_refs if item["document_id"]}, key=str)
        document_version_ids = sorted({item["document_version_id"] for item in exact_refs if item["document_version_id"]}, key=str)
        region = event.metadata_json.get("region")
        if not isinstance(region, str) or region not in regions:
            continue
        signals.append(CorrelationSignalRead(
            signal_id=event.id, event_id=event.id, event_type=event.type,
            region=region, observed_at=event.started_at, source_id=event.source_id,
            event_evidence_ids=event_evidence_ids[:100],
            document_ids=document_ids[:100],
            document_version_ids=document_version_ids[:100],
            chunk_ids=sorted({item.chunk_id for item in exact_rows if item.chunk_id}, key=str)[:100],
            omitted_event_evidence_ids=max(0, len(event_evidence_ids) - 100),
            omitted_document_ids=max(0, len(document_ids) - 100),
            omitted_document_version_ids=max(0, len(document_version_ids) - 100),
        ))
        if len(signals) > limit:
            break
    truncated = len(signals) > limit or len(rows) > 1_000
    return CorrelationSignalPage(items=signals[:limit], truncated=truncated)


async def _list_partition(
    session: AsyncSession, query: TimelineQuery, partition: int, key: Any, limit: int, *, scope: Scope,
) -> list[Event]:
    """Read a stable occurrence partition with shared visibility and type filters applied."""
    statement = select(Event).where(Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None)).where(
        (Event.origin == "manual") | Event.id.in_(select(EventEvidence.event_id).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == Event.id, EventEvidence.document_version_id.is_not(None),
            EventEvidence.chunk_id.is_not(None),
        ))
    )
    if query.source_id is not None:
        statement = statement.where(Event.source_id == query.source_id)
    if query.type is not None:
        # Escape SQL LIKE metacharacters so the user value remains a literal substring.
        type_pattern = query.type.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(Event.type.ilike(f"%{type_pattern}%", escape="\\"))
    if partition == 0:
        statement = statement.where(Event.date_precision == "timed")
        if query.date_from is not None:
            start, _ = day_window(query.date_from, query.timezone)
            assert query.date_to is not None  # validated together with date_from
            _, end = day_window(query.date_to - timedelta(days=1), query.timezone)
            statement = statement.where(Event.started_at >= start, Event.started_at < end)
        if query.entity_id is not None:
            statement = statement.where(Event.id.in_(select(EventParticipant.event_id).where(EventParticipant.entity_id == query.entity_id)))
        if key is not None:
            instant, identifier = key
            statement = statement.where(tuple_(Event.started_at, Event.id) < (datetime.fromisoformat(str(instant)), UUID(str(identifier))))
        statement = statement.order_by(desc(Event.started_at), desc(Event.id))
    elif partition == 1:
        statement = statement.where(Event.date_precision == "date")
        if query.date_from is not None:
            statement = statement.where(Event.occurred_date >= query.date_from, Event.occurred_date < query.date_to)
        if query.entity_id is not None:
            statement = statement.where(Event.id.in_(select(EventParticipant.event_id).where(EventParticipant.entity_id == query.entity_id)))
        if key is not None:
            day, identifier = key
            statement = statement.where(tuple_(Event.occurred_date, Event.id) < (date.fromisoformat(str(day)), UUID(str(identifier))))
        statement = statement.order_by(desc(Event.occurred_date), desc(Event.id))
    else:
        statement = statement.where(Event.date_precision == "unknown")
        if query.precision != "unknown" and query.date_from is not None:
            statement = statement.where(false())
        if query.entity_id is not None:
            statement = statement.where(Event.id.in_(select(EventParticipant.event_id).where(EventParticipant.entity_id == query.entity_id)))
        if key is not None:
            instant, identifier = key
            statement = statement.where(tuple_(Event.created_at, Event.id) < (datetime.fromisoformat(str(instant)), UUID(str(identifier))))
        statement = statement.order_by(desc(Event.created_at), desc(Event.id))
    return list((await session.scalars(statement.limit(limit))).all())


async def list_events(
    session: AsyncSession, *, limit: int = 50, cursor: str | None = None, source_id: UUID | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> EventPage:
    """Return bounded events with stable timed/date/unknown cursor partitions."""
    query = TimelineQuery(source_id=source_id)
    return await _list_page(session, query, limit, cursor, scope=scope,
                             multi_workspace_enabled=multi_workspace_enabled)


async def list_timeline(
    session: AsyncSession, query: TimelineQuery, *, limit: int = 50, cursor: str | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> TimelinePage:
    """Return timeline pages in timed, date-only, then unknown partition order."""
    return await _list_page(session, query, limit, cursor, scope=scope,
                             multi_workspace_enabled=multi_workspace_enabled)


async def _list_page(
    session: AsyncSession, query: TimelineQuery, limit: int, cursor: str | None, *, scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelinePage:
    """Apply the shared finite cursor algorithm and detached DTO projection."""
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError("page size must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    filters = {**query.model_dump(mode="json"), "workspace_id": str(scope.workspace_id)}
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    position = _cursor_decode(cursor, fingerprint)
    partition = int(position["p"])
    key = position["k"]
    items: list[Event] = []
    next_position: dict[str, object] | None = None
    while partition < 3 and len(items) < limit:
        if query.precision == "timed" and partition != 0 or query.precision == "date" and partition != 1 or query.precision == "unknown" and partition != 2:
            rows = []
        else:
            rows = await _list_partition(session, query, partition, key, limit - len(items) + 1, scope=scope)
        room = limit - len(items)
        items.extend(rows[:room])
        if len(rows) > room:
            last = rows[room - 1]
            value = last.started_at if partition == 0 else last.occurred_date if partition == 1 else last.created_at
            assert value is not None
            next_position = {"v": 1, "f": fingerprint, "p": partition, "k": [value.isoformat(), str(last.id)]}
            break
        partition += 1
        key = None
    if next_position is None and partition < 3 and len(items) == limit:
        next_position = {"v": 1, "f": fingerprint, "p": partition, "k": None}
    return TimelinePage(items=[await _event_read(session, row, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled) for row in items],
        next_cursor=_cursor_encode(next_position) if next_position else None)


async def _evidence_rows(
    session: AsyncSession, pairs: list[tuple[UUID, UUID]], *, for_write: bool, scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Resolve exact version/chunk pairs through the documents owner boundary."""
    if not pairs:
        return []
    refs = await documents.read_evidence_refs(session, pairs, for_write=for_write, scope=scope,
                                              multi_workspace_enabled=multi_workspace_enabled)
    if len(refs) != len(pairs):
        raise ValueError("event evidence is missing, inactive, or no longer authorized")
    return refs


async def _read_evidence_closure(
    session: AsyncSession, pairs: list[tuple[UUID, UUID]], *, scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Read a unique event support closure of at most 200 pairs in owner-sized batches.

    A correction can combine 100 old and 100 replacement references; extraction
    can span 150 references. Missing or unauthorized evidence propagates the
    owner's validation error; callers retain complete checks and lock ordering.
    This helper never acquires write locks or truncates support.
    """
    if len(pairs) > 200 or len(set(pairs)) != len(pairs):
        raise ValueError("Event evidence closure must contain at most 200 unique references")
    refs = []
    # Owner reads cap each request at 100; splitting preserves the full closure
    # without weakening that shared boundary or changing source lock ordering.
    for offset in range(0, len(pairs), 100):
        refs.extend(await documents.read_evidence_refs(session, pairs[offset:offset + 100], scope=scope,
                                                       multi_workspace_enabled=multi_workspace_enabled))
    return refs


async def _set_participants(
    session: AsyncSession, event_id: UUID, values: list[Any], *, origin: str, scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Replace participant links after validating every canonical entity reference."""
    await session.execute(delete(EventParticipant).where(
        EventParticipant.event_id == event_id,
        EventParticipant.event_id.in_(select(Event.id).where(
            Event.workspace_id == scope.workspace_id, Event.id == event_id,
        )),
    ))
    entity_ids = sorted({item.entity_id for item in values}, key=str)
    refs = await entities.get_entity_refs(
        session, entity_ids, for_write=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) if entity_ids else []
    if len(refs) != len(entity_ids):
        raise ValueError("participant entity is missing or redirected")
    for item in values:
        session.add(EventParticipant(event_id=event_id, entity_id=item.entity_id, role=item.role,
                                     metadata_json=item.metadata, origin=origin))


async def _remove_unsupported_derived_participants(
    session: AsyncSession, event_id: UUID, *, scope: Scope,
) -> None:
    """Remove derived participant roles with no exact surviving event-evidence support.

    Evidence replacement may cascade its participant-support rows. Manual links
    remain owner-authored, while each derived link must retain at least one
    exact supporting chunk from the event's current evidence set.
    """
    unsupported_ids = list((await session.scalars(select(EventParticipant.id).where(
        EventParticipant.event_id == event_id,
        EventParticipant.event_id.in_(select(Event.id).where(
            Event.workspace_id == scope.workspace_id, Event.id == event_id,
        )),
        EventParticipant.origin == "derived",
        ~EventParticipant.id.in_(select(ParticipantEvidence.participant_id)),
    ))).all())
    if unsupported_ids:
        await session.execute(delete(EventParticipant).where(EventParticipant.id.in_(unsupported_ids)))


async def create_event(
    session: AsyncSession, payload: EventCreate, *, actor_id: int, scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead:
    """Create a manual event and flush its exact evidence, participants, and replay atomically.

    This caller-owned HTTP command commits through ``commit_with_replay``;
    reusable support helpers only flush and never commit.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    pairs = list(payload.evidence)
    refs = await _evidence_rows(session, pairs, for_write=bool(pairs), scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled)
    source_ids = sorted({ref.source_id for ref in refs}, key=str)
    if len(source_ids) > 1:
        raise ValueError("manual event evidence must belong to one source")
    # Resolve owner observation only at creation; derived occurrence is never inferred here.
    event = Event(
        workspace_id=scope.workspace_id, source_id=source_ids[0] if source_ids else None,
        type=payload.type, subtype=payload.subtype,
        title=payload.title, summary=payload.summary, importance_score=payload.importance_score,
        confidence=payload.confidence, metadata_json=payload.metadata, origin="manual",
        date_precision=payload.date_precision, started_at=payload.started_at, ended_at=payload.ended_at,
        occurred_date=payload.occurred_date, end_date=payload.end_date,
        occurrence_timezone=payload.occurrence_timezone, observed_at=payload.observed_at or datetime.now(UTC),
        valid_from=payload.valid_from, valid_to=payload.valid_to,
    )
    session.add(event)
    await session.flush()
    for ref in refs:
        session.add(EventEvidence(
            workspace_id=scope.workspace_id, event_id=event.id, source_id=ref.source_id, document_id=ref.document_id,
            document_version_id=ref.document_version_id, chunk_id=ref.chunk_id,
            version_number=ref.version_number, observed_at=ref.observed_at,
            title_snapshot=ref.title, url_snapshot=ref.canonical_url,
            metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
            evidence_metadata={},
        ))
    await _set_participants(session, event.id, payload.participants, origin="manual", scope=scope,
                            multi_workspace_enabled=multi_workspace_enabled)
    await _schedule_temporal_event(session, event, ["created"], scope=scope,
                                   multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(session, [make_timeline_change(event.id, event.revision, scope=scope)],
                             scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _event_read(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def update_event(
    session: AsyncSession, event_id: UUID, payload: EventPatch, *, actor_id: int, scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead | None:
    """Apply a revision-fenced owner correction after locking its immutable support closure.

    Locks source, document, canonical participant and event rows in that order.
    Any revision or support change while those locks are acquired aborts before
    mutation; the caller commits the correction, audit and replay atomically.
    Explicit null clears nullable fields, participant links, or evidence lists.
    """
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    initial = (await session.execute(select(Event.revision, Event.origin, Event.deleted_at).where(
        Event.workspace_id == scope.workspace_id, Event.id == event_id, Event.deleted_at.is_(None),
    ))).one_or_none()
    if initial is None:
        return None
    initial_evidence = list((await session.execute(select(
        EventEvidence.id, EventEvidence.source_id, EventEvidence.document_id,
        EventEvidence.document_version_id, EventEvidence.chunk_id,
    ).where(EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event_id).order_by(EventEvidence.id))).all())
    initial_participants = list((await session.execute(select(
        EventParticipant.id, EventParticipant.entity_id, EventParticipant.role, EventParticipant.origin,
    ).where(EventParticipant.event_id == event_id,
            EventParticipant.event_id.in_(select(Event.id).where(Event.workspace_id == scope.workspace_id))).order_by(EventParticipant.id))).all())
    fields = payload.model_fields_set
    supplied = payload.model_dump(exclude_unset=True, exclude={"expected_revision", "reason", "participants", "evidence"})
    old_pairs = [(row.document_version_id, row.chunk_id) for row in (await session.scalars(
        select(EventEvidence).where(EventEvidence.workspace_id == scope.workspace_id,
                                   EventEvidence.event_id == event_id).order_by(EventEvidence.id)
    )).all() if row.document_version_id is not None and row.chunk_id is not None]
    new_pairs = payload.evidence if "evidence" in fields else None
    all_pairs = list(dict.fromkeys([*old_pairs, *(new_pairs or [])]))
    refs_before = await _read_evidence_closure(session, all_pairs, scope=scope,
                                               multi_workspace_enabled=multi_workspace_enabled)
    source_ids = sorted({item.source_id for item in refs_before}, key=str)
    from modules.sources import public as sources
    for source_id in source_ids:
        if await sources.lock_source(session, source_id, scope=scope,
                multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=fence) is None:
            raise ValueError("event evidence source is unavailable")
    document_ids = sorted({item.document_id for item in refs_before}, key=str)
    # Old and replacement support can span 200 owners. Keep the global sorted
    # order across batches and retain every row lock in this same transaction.
    for offset in range(0, len(document_ids), 100):
        await documents.lock_document_ids(session, document_ids[offset:offset + 100], scope=scope,
                                          multi_workspace_enabled=multi_workspace_enabled)
    refs_after = await _read_evidence_closure(session, all_pairs, scope=scope,
                                              multi_workspace_enabled=multi_workspace_enabled)
    if refs_after != refs_before:
        raise ValueError("stale event evidence closure; retry the correction")
    participant_values = (payload.participants or []) if "participants" in fields else []
    old_entity_ids = {row.entity_id for row in initial_participants}
    entity_ids = sorted(old_entity_ids | {item.entity_id for item in participant_values}, key=str)
    if entity_ids:
        canonical_refs = []
        for offset in range(0, len(entity_ids), 100):
            canonical_refs.extend(await entities.get_entity_refs(session, entity_ids[offset:offset + 100],
                for_write=True, scope=scope, multi_workspace_enabled=multi_workspace_enabled))
        if [item.requested_id for item in canonical_refs] != entity_ids or any(
            item.canonical_id != item.requested_id for item in canonical_refs
        ):
            raise ValueError("participant entity is missing or redirected; retry the correction")
    event = await session.scalar(select(Event).where(Event.workspace_id == scope.workspace_id,
        Event.id == event_id).with_for_update().execution_options(populate_existing=True))
    if event is None or event.deleted_at is not None:
        return None
    current_evidence = list((await session.execute(select(
        EventEvidence.id, EventEvidence.source_id, EventEvidence.document_id,
        EventEvidence.document_version_id, EventEvidence.chunk_id,
    ).where(EventEvidence.workspace_id == scope.workspace_id,
            EventEvidence.event_id == event_id).order_by(EventEvidence.id))).all())
    current_participants = list((await session.execute(select(
        EventParticipant.id, EventParticipant.entity_id, EventParticipant.role, EventParticipant.origin,
    ).where(EventParticipant.event_id == event_id,
            EventParticipant.event_id.in_(select(Event.id).where(Event.workspace_id == scope.workspace_id))).order_by(EventParticipant.id))).all())
    if (
        event.revision != initial.revision or event.origin != initial.origin
        or event.deleted_at != initial.deleted_at
        or current_evidence != initial_evidence or current_participants != initial_participants
        or event.revision != payload.expected_revision
    ):
        raise ValueError("stale event revision or support closure; retry the correction")
    prior = event.revision
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
        type=event.type, subtype=event.subtype, title=event.title, summary=event.summary,
        importance_score=event.importance_score, confidence=event.confidence, metadata=event.metadata_json,
        date_precision=event.date_precision, started_at=event.started_at, ended_at=event.ended_at,
        occurred_date=event.occurred_date, end_date=event.end_date,
        occurrence_timezone=event.occurrence_timezone, observed_at=event.observed_at,
        valid_from=event.valid_from, valid_to=event.valid_to,
    )
    if "participants" in fields:
        await _set_participants(session, event.id, participant_values, origin="manual", scope=scope,
                                multi_workspace_enabled=multi_workspace_enabled)
        changed["participants"] = [item.model_dump(mode="json") for item in participant_values]
        if event.origin == "derived":
            event.owner_fields = sorted(set(event.owner_fields) | {"participants"})
    if "evidence" in fields:
        evidence_pairs = payload.evidence or []
        refs = await _evidence_rows(session, evidence_pairs, for_write=bool(evidence_pairs), scope=scope,
                                    multi_workspace_enabled=multi_workspace_enabled)
        evidence_sources = {ref.source_id for ref in refs}
        if len(evidence_sources) > 1:
            raise ValueError("event evidence must belong to one source")
        await session.execute(delete(EventEvidence).where(EventEvidence.workspace_id == scope.workspace_id,
                                                          EventEvidence.event_id == event.id))
        for ref in refs:
            session.add(EventEvidence(workspace_id=scope.workspace_id, event_id=event.id, source_id=ref.source_id, document_id=ref.document_id,
                document_version_id=ref.document_version_id, chunk_id=ref.chunk_id,
                version_number=ref.version_number, observed_at=ref.observed_at, title_snapshot=ref.title,
                url_snapshot=ref.canonical_url, evidence_metadata={},
                metadata_is_version_snapshot=ref.metadata_is_version_snapshot))
        event.source_id = next(iter(evidence_sources), None)
        if event.origin == "derived":
            await _remove_unsupported_derived_participants(session, event.id, scope=scope)
        changed["evidence"] = [f"{ref.document_version_id}:{ref.chunk_id}" for ref in refs]
        if event.origin == "derived":
            event.owner_fields = sorted(set(event.owner_fields) | {"evidence"})
    if not changed:
        raise ValueError("event correction contains no changes")
    event.revision += 1
    event.updated_at = datetime.now(UTC)
    session.add(EventAudit(workspace_id=scope.workspace_id, event_id=event.id, actor_id=actor_id, reason=payload.reason,
        prior_revision=prior, resulting_revision=event.revision, changed_json=changed))
    await _schedule_temporal_event(session, event, list(changed), scope=scope,
                                   multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(session, [make_timeline_change(event.id, event.revision, scope=scope)],
                             scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return await _event_read(session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


def _normalize_optional_instant(value: Any) -> Any:
    """Normalize explicit-offset datetimes for merged PATCH payload values."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include an explicit UTC offset")
        return value.astimezone(UTC)
    return value


async def delete_event(
    session: AsyncSession, event_id: UUID, *, expected_revision: int, reason: str, actor_id: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Tombstone a revision-fenced event and suppress its exact derived proposal before replay commit."""
    fence = await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    event = await session.scalar(select(Event).where(
        Event.workspace_id == scope.workspace_id, Event.id == event_id,
    ).with_for_update())
    if event is None or event.deleted_at is not None:
        return False
    if event.revision != expected_revision:
        raise ValueError("event revision is stale")
    if event.origin == "derived":
        supports = list((await session.scalars(select(EventEvidence).where(
            EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
            EventEvidence.document_version_id.is_not(None), EventEvidence.candidate_hash.is_not(None)
        ))).all())
        unique_supports = {(item.document_version_id, item.candidate_hash): item for item in supports}
        for item in unique_supports.values():
            session.add(EventSuppression(workspace_id=scope.workspace_id, document_id=item.document_id, document_version_id=item.document_version_id,
                source_id=item.source_id, source_generation=item.source_generation, candidate_hash=item.candidate_hash))
    prior = event.revision
    event.deleted_at = datetime.now(UTC)
    event.revision += 1
    session.add(EventAudit(workspace_id=scope.workspace_id, event_id=event.id, actor_id=actor_id, reason=reason,
        prior_revision=prior, resulting_revision=event.revision, changed_json={"deleted": True}))
    await _schedule_temporal_event(session, event, ["deleted"], deleted=True, scope=scope,
                                   multi_workspace_enabled=multi_workspace_enabled)
    await commit_with_replay(session, [make_timeline_change(event.id, event.revision, deleted=True, scope=scope)],
                             scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=fence)
    return True


async def schedule_extraction_work(
    session: AsyncSession, ready: Any, extractor_version: str, prompt_version: str, *, scope: Scope,
    multi_workspace_enabled: bool,
) -> UUID:
    """Idempotently create timeline extraction work and flush without committing the caller transaction."""
    from sqlalchemy.dialects.postgresql import insert

    from modules.timeline.models import TimelineExtractionWork
    statement = insert(TimelineExtractionWork).values(
        workspace_id=scope.workspace_id, document_id=ready.document_id, document_version_id=ready.document_version_id,
        source_id=ready.source_id, source_generation=ready.source_generation,
        extractor_version=extractor_version, prompt_version=prompt_version,
    ).on_conflict_do_nothing(constraint="uq_timeline_extraction_work_identity").returning(TimelineExtractionWork.id)
    work_id = await session.scalar(statement)
    if work_id is None:
        work_id = await session.scalar(select(TimelineExtractionWork.id).where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.document_version_id == ready.document_version_id,
            TimelineExtractionWork.source_generation == ready.source_generation,
            TimelineExtractionWork.extractor_version == extractor_version,
            TimelineExtractionWork.prompt_version == prompt_version,
        ))
    assert work_id is not None  # the conflicting identity row exists
    await session.flush()
    # GitHub events are produced deterministically by the connector mapper and are canonical;
    # model extraction would only add duplicates. Terminal-block the work (an error code the
    # recheck/requeue paths ignore) so every scheduler, including recovery, skips it.
    from modules.sources import public as sources
    detached = await sources.get_connector_source(session, ready.source_id, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled)
    if detached is not None and detached.provider == "github":
        work = await session.scalar(select(TimelineExtractionWork).where(
            TimelineExtractionWork.workspace_id == scope.workspace_id,
            TimelineExtractionWork.id == work_id).with_for_update())
        if work is not None and work.status not in {"succeeded", "blocked"}:
            work.status, work.error_code = "blocked", "deterministic_provider"
            work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
            work.dependency_fingerprint = None
            work.lease_owner = work.lease_expires_at = None
            await session.flush()
    return work_id


async def claim_extraction_work(
    session: AsyncSession, work_id: UUID, lease_owner: str, now: datetime, *, scope: Scope,
    multi_workspace_enabled: bool,
) -> Any | None:
    """Claim due work or terminalize an exhausted expired lease; flush without committing."""
    from datetime import timedelta

    from modules.timeline.models import TimelineExtractionWork
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id, TimelineExtractionWork.id == work_id,
        (TimelineExtractionWork.status == "pending") | (
            (TimelineExtractionWork.status == "running") & (TimelineExtractionWork.lease_expires_at <= now)
        ),
        TimelineExtractionWork.next_attempt_at <= now,
    ).with_for_update(skip_locked=True))
    if work is None:
        return None
    if work.status == "running" and work.attempt >= 5:
        work.status, work.error_code = "failed", "lease_attempts_exhausted"
        work.lease_owner = work.lease_expires_at = None
        await session.flush()
        return None
    work.status, work.attempt = "running", work.attempt + 1
    work.lease_owner, work.lease_expires_at = lease_owner, now + timedelta(seconds=110)
    work.error_code = None
    await session.flush()
    return work


async def list_recoverable_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Return bounded due pending work and expired leases for claim or exhaustion handling."""
    from modules.timeline.models import TimelineExtractionWork
    if not 1 <= limit <= 100:
        raise ValueError("Timeline recovery page must be between 1 and 100")
    now = datetime.now(UTC)
    return list((await session.scalars(select(TimelineExtractionWork.id).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        ((TimelineExtractionWork.status == "pending") & (TimelineExtractionWork.next_attempt_at <= now))
        | ((TimelineExtractionWork.status == "running") & (TimelineExtractionWork.lease_expires_at <= now)),
    ).order_by(TimelineExtractionWork.next_attempt_at, TimelineExtractionWork.id).limit(limit))).all())


async def list_blocked_extraction_work(
    session: AsyncSession, limit: int = 25, *, scope: Scope, multi_workspace_enabled: bool,
) -> list[tuple[UUID, UUID, int, str, str]]:
    """List a bounded page of retryable policy-blocked work and its dependency fence."""
    from modules.timeline.models import TimelineExtractionWork
    if not 1 <= limit <= 100:
        raise ValueError("Blocked timeline recovery page must be between 1 and 100")
    rows = (await session.execute(select(
        TimelineExtractionWork.id, TimelineExtractionWork.document_version_id,
        TimelineExtractionWork.source_generation, TimelineExtractionWork.error_code,
        TimelineExtractionWork.dependency_fingerprint,
    ).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.status == "blocked",
        TimelineExtractionWork.error_code.in_(("ai_policy_denied", "structured_unsupported")),
        TimelineExtractionWork.dependency_fingerprint.is_not(None),
        TimelineExtractionWork.next_attempt_at <= datetime.now(UTC),
    ).order_by(TimelineExtractionWork.updated_at, TimelineExtractionWork.id).limit(limit))).all()
    # error_code is an in_() match and dependency_fingerprint is filtered non-NULL above.
    return [(row[0], row[1], row[2], cast(str, row[3]), cast(str, row[4])) for row in rows]


async def requeue_blocked_extraction_work(
    session: AsyncSession, work_id: UUID, previous_fingerprint: str, current_fingerprint: str, *,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Requeue policy-blocked work only if its stored dependency fence still matches."""
    from modules.timeline.models import TimelineExtractionWork
    if previous_fingerprint == current_fingerprint:
        return False
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.id == work_id, TimelineExtractionWork.status == "blocked",
        TimelineExtractionWork.dependency_fingerprint == previous_fingerprint,
    ).with_for_update())
    if work is None:
        return False
    work.status, work.attempt = "pending", 0
    work.next_attempt_at = datetime.now(UTC)
    work.error_code = work.dependency_fingerprint = None
    work.lease_owner = work.lease_expires_at = None
    return True


async def defer_blocked_extraction_recheck(
    session: AsyncSession, work_id: UUID, fingerprint: str, *, minutes: int = 15,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Delay the next recheck only while a policy block retains the observed dependency fence."""
    from modules.timeline.models import TimelineExtractionWork
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.id == work_id, TimelineExtractionWork.status == "blocked",
        TimelineExtractionWork.dependency_fingerprint == fingerprint,
    ).with_for_update())
    if work is not None:
        work.next_attempt_at = datetime.now(UTC) + timedelta(minutes=minutes)


async def set_extraction_work_error(session: AsyncSession, work_id: UUID, lease_owner: str,
                                    error_code: str, *, blocked: bool = False,
                                    dependency_fingerprint: str | None = None, scope: Scope,
                                    multi_workspace_enabled: bool) -> None:
    """Persist lease-owned retry state; blocked retries fence against the attempted dependencies.

    The caller retains the request's original dependency fingerprint when it
    discards stale output so a newly permitted configuration can be retried.
    """
    from datetime import timedelta

    from modules.timeline.models import TimelineExtractionWork
    now = datetime.now(UTC)
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.id == work_id, TimelineExtractionWork.status == "running",
        TimelineExtractionWork.lease_owner == lease_owner, TimelineExtractionWork.lease_expires_at > now,
    ).with_for_update())
    if work is None:
        return
    work.status = "blocked" if blocked else ("failed" if work.attempt >= 5 else "pending")
    work.error_code = error_code[:64]
    work.dependency_fingerprint = dependency_fingerprint if blocked else None
    work.next_attempt_at = now + timedelta(minutes=15) if blocked else now + timedelta(minutes=min(2 ** work.attempt, 60))
    work.lease_owner = work.lease_expires_at = None


async def block_local_only_extraction_work(
    session: AsyncSession, work_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Mark extraction ineligible while the source remains local-only, without a recheck fence.

    Configuration changes cannot authorize provider egress from a local-only
    source, so this durable state has no scheduled policy recheck.
    """
    from modules.timeline.models import TimelineExtractionWork
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id, TimelineExtractionWork.id == work_id,
    ).with_for_update())
    if work is not None and work.status != "succeeded":
        work.status, work.error_code = "blocked", "local_only_source"
        work.next_attempt_at = datetime.max.replace(tzinfo=UTC)
        work.dependency_fingerprint = None
        work.lease_owner = work.lease_expires_at = None
        await session.flush()


async def finish_extraction_work(session: AsyncSession, work_id: UUID, lease_owner: str,
                                 proposals: list[dict[str, object]], model: str | None, *, scope: Scope,
                                 multi_workspace_enabled: bool) -> bool:
    """Persist bounded structured proposals without raw source chunks under a live lease."""
    from modules.timeline.models import TimelineExtractionResult, TimelineExtractionWork
    now = datetime.now(UTC)
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.id == work_id, TimelineExtractionWork.status == "running",
        TimelineExtractionWork.lease_owner == lease_owner, TimelineExtractionWork.lease_expires_at > now,
    ).with_for_update())
    if work is None:
        return False
    result = await session.scalar(select(TimelineExtractionResult).where(
        TimelineExtractionResult.work_id == work.id  # work was selected above under this workspace
    ).with_for_update())
    if result is None:
        session.add(TimelineExtractionResult(work_id=work.id, proposals_json=proposals, model=model))
    else:
        result.proposals_json, result.model = proposals, model
    work.status, work.lease_owner, work.lease_expires_at = "succeeded", None, None
    await session.flush()
    return True


async def publish_extracted_events(session: AsyncSession, *, work_id: UUID, lease_owner: str,
                                   ready: Any, proposals: Any, model: str | None,
                                   membership_revisions: dict[UUID, int], scope: Scope,
                                   multi_workspace_enabled: bool) -> bool:
    """Publish bounded event proposals and exact evidence under caller-owned source/document fences.

    Resolves and locks participant entities after inference, then event rows.
    Membership revisions captured before inference must still match the locked
    canonical revisions. Existing owner fields win; if proposed unowned
    temporal fields conflict with that merged state, the unowned temporal
    refresh group is skipped and named while safe fields, evidence, and results
    publish.
    It flushes only; the worker commits results, replay and success atomically.
    """
    from hashlib import sha256

    from sqlalchemy.dialects.postgresql import insert

    from modules.timeline.extraction import EXTRACTOR_VERSION, PROMPT_VERSION
    from modules.timeline.models import TimelineExtractionWork
    work = await session.scalar(select(TimelineExtractionWork).where(
        TimelineExtractionWork.workspace_id == scope.workspace_id,
        TimelineExtractionWork.id == work_id, TimelineExtractionWork.status == "running",
        TimelineExtractionWork.lease_owner == lease_owner,
        TimelineExtractionWork.lease_expires_at > datetime.now(UTC),
    ).with_for_update())
    if work is None or work.source_generation != ready.source_generation or work.document_version_id != ready.document_version_id:
        return False
    chunk_ids = sorted({chunk for item in proposals.events for chunk in item.evidence_chunk_ids}, key=str)
    refs = await documents.read_extraction_evidence_refs(
        session, document_id=ready.document_id, document_version_id=ready.document_version_id,
        source_id=ready.source_id, source_generation=ready.source_generation, chunk_ids=chunk_ids,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ) if chunk_ids else []
    if refs is None:
        raise ValueError("event evidence is no longer current")
    evidence = await _read_evidence_closure(session, [(item.document_version_id, item.chunk_id) for item in refs],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(evidence) != len(refs):
        raise ValueError("event evidence snapshot is unavailable")
    evidence_by_chunk = {item.chunk_id: item for item in evidence}
    memberships = []
    for offset in range(0, len(chunk_ids), 100):
        memberships.extend(await entities.list_version_membership_refs(session, ready.document_version_id,
            chunk_ids[offset:offset + 100], scope=scope, multi_workspace_enabled=multi_workspace_enabled))
    membership_by_id = {item.membership_id: item for item in memberships}
    allowed = set(chunk_ids)
    normalized: list[tuple[Any, str, dict[tuple[UUID, str], Any]]] = []
    entity_ids: set[UUID] = set()
    identity = f"{ready.document_version_id}:{ready.source_generation}:{EXTRACTOR_VERSION}:{PROMPT_VERSION}"
    for proposal in proposals.events:
        if not set(proposal.evidence_chunk_ids) <= allowed:
            raise ValueError("model returned unknown event evidence")
        participants: dict[tuple[UUID, str], Any] = {}
        for participant in proposal.participants:
            membership = membership_by_id.get(participant.membership_id)
            if membership is None or membership.chunk_id not in participant.chunk_ids:
                raise ValueError("model returned an unknown participant membership")
            if not set(participant.chunk_ids) <= set(proposal.evidence_chunk_ids):
                raise ValueError("participant support must be a subset of event evidence")
            exact = {item.chunk_id for item in memberships if item.entity_id == membership.entity_id
                     and item.chunk_id in participant.chunk_ids}
            if exact != set(participant.chunk_ids):
                raise ValueError("participant role lacks exact canonical membership support")
            key = (membership.entity_id, participant.role)
            previous = participants.get(key)
            if previous is not None:
                participant = previous.model_copy(update={
                    "chunk_ids": sorted(set(previous.chunk_ids) | set(participant.chunk_ids), key=str),
                })
            participants[key] = participant
            entity_ids.add(membership.entity_id)
        participant_identity = [
            (str(entity_id), role, sorted(map(str, item.chunk_ids)))
            for (entity_id, role), item in participants.items()
        ]
        data = proposal.model_dump(mode="json")
        fingerprint = {
            "type": proposal.type, "title": proposal.title,
            "occurrence": [data.get(name) for name in ("date_precision", "started_at", "ended_at", "occurred_date", "end_date")],
            "participants": sorted(participant_identity),
            "evidence": sorted(map(str, proposal.evidence_chunk_ids)),
        }
        candidate_hash = sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        normalized.append((proposal, candidate_hash, participants))
    unique_by_candidate: dict[str, tuple[Any, str, dict[tuple[UUID, str], Any]]] = {}
    for item in normalized:
        previous = unique_by_candidate.get(item[1])
        item_json = json.dumps(item[0].model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        previous_json = json.dumps(previous[0].model_dump(mode="json"), sort_keys=True, separators=(",", ":")) if previous else ""
        if previous is None or item_json < previous_json:
            unique_by_candidate[item[1]] = item
    normalized = [unique_by_candidate[key] for key in sorted(unique_by_candidate)]
    # No graph lock is held during provider inference; acquire sorted canonical
    # entity revisions and then events only after a validated response exists.
    if entity_ids:
        entity_refs = await entities.get_entity_refs(session, sorted(entity_ids, key=str), for_write=True,
            scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if {item.canonical_id for item in entity_refs} != entity_ids:
            raise ValueError("participant canonical identity changed during extraction")
        expected_revisions = {
            membership_by_id[participant.membership_id].entity_id:
            membership_revisions[participant.membership_id]
            for proposal in proposals.events for participant in proposal.participants
        }
        if any(item.revision != expected_revisions.get(item.canonical_id) for item in entity_refs):
            raise ValueError("participant canonical revision changed during extraction")
    candidates = [item[1] for item in normalized]
    existing = list((await session.scalars(select(Event).where(
        Event.workspace_id == scope.workspace_id, Event.extraction_identity == identity,
        Event.candidate_hash.in_(candidates)
    ).order_by(Event.id).with_for_update())).all()) if candidates else []
    by_hash = {item.candidate_hash: item for item in existing}
    suppressed = set((await session.scalars(select(EventSuppression.candidate_hash).where(
        EventSuppression.workspace_id == scope.workspace_id,
        EventSuppression.document_version_id == ready.document_version_id,
        EventSuppression.candidate_hash.in_(candidates),
    ))).all()) if candidates else set()
    published: list[tuple[Event, Any]] = []
    refresh_conflicts: dict[str, list[str]] = {}
    for proposal, candidate_hash, participants in normalized:
        if candidate_hash in suppressed:
            continue
        event = by_hash.get(candidate_hash)
        if event is None:
            event = Event(
                workspace_id=scope.workspace_id, source_id=ready.source_id, type=proposal.type, subtype=proposal.subtype,
                title=proposal.title, summary=proposal.summary, importance_score=proposal.importance_score,
                confidence=proposal.confidence, metadata_json={}, origin="derived",
                date_precision=proposal.date_precision,
                started_at=proposal.started_at.astimezone(UTC) if proposal.started_at else None,
                ended_at=proposal.ended_at.astimezone(UTC) if proposal.ended_at else None,
                occurred_date=proposal.occurred_date, end_date=proposal.end_date,
                occurrence_timezone=proposal.occurrence_timezone, observed_at=ready.observed_at,
                valid_from=proposal.valid_from.astimezone(UTC) if proposal.valid_from else None,
                valid_to=proposal.valid_to.astimezone(UTC) if proposal.valid_to else None,
                extraction_identity=identity, candidate_hash=candidate_hash,
            )
            session.add(event)
            await session.flush()
        else:
            owners = set(event.owner_fields)
            refreshed = {
                "title": proposal.title, "summary": proposal.summary, "type": proposal.type,
                "subtype": proposal.subtype, "importance_score": proposal.importance_score,
                "confidence": proposal.confidence, "date_precision": proposal.date_precision,
                "started_at": proposal.started_at.astimezone(UTC) if proposal.started_at else None,
                "ended_at": proposal.ended_at.astimezone(UTC) if proposal.ended_at else None,
                "occurred_date": proposal.occurred_date, "end_date": proposal.end_date,
                "occurrence_timezone": proposal.occurrence_timezone,
                "valid_from": proposal.valid_from.astimezone(UTC) if proposal.valid_from else None,
                "valid_to": proposal.valid_to.astimezone(UTC) if proposal.valid_to else None,
            }
            pending_updates = {
                field: value for field, value in refreshed.items()
                if field not in owners and getattr(event, field) != value
            }
            base_values = {
                "type": event.type, "subtype": event.subtype, "title": event.title,
                "summary": event.summary, "importance_score": event.importance_score,
                "confidence": event.confidence, "metadata": event.metadata_json,
                "date_precision": event.date_precision, "started_at": event.started_at,
                "ended_at": event.ended_at, "occurred_date": event.occurred_date,
                "end_date": event.end_date, "occurrence_timezone": event.occurrence_timezone,
                "observed_at": event.observed_at, "valid_from": event.valid_from,
                "valid_to": event.valid_to,
            }
            temporal_fields = {
                "date_precision", "started_at", "ended_at", "occurred_date", "end_date",
                "occurrence_timezone", "valid_from", "valid_to",
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
                    field: value for field, value in pending_updates.items()
                    if field not in temporal_fields
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
        published.append((event, proposal))
        if "evidence" not in event.owner_fields:
            for chunk_id in proposal.evidence_chunk_ids:
                ref = evidence_by_chunk[chunk_id]
                await session.execute(insert(EventEvidence).values(
                    workspace_id=scope.workspace_id, event_id=event.id, source_id=ref.source_id, document_id=ref.document_id,
                    document_version_id=ref.document_version_id, chunk_id=ref.chunk_id,
                    version_number=ref.version_number, source_generation=ready.source_generation,
                    extraction_identity=identity, candidate_hash=candidate_hash,
                    confidence=proposal.confidence, extracted_at=datetime.now(UTC),
                    observed_at=ref.observed_at, title_snapshot=ref.title, url_snapshot=ref.canonical_url,
                    evidence_metadata={}, excerpt=ref.excerpt,
                    metadata_is_version_snapshot=ref.metadata_is_version_snapshot,
                ).on_conflict_do_nothing(constraint="uq_timeline_event_evidence"))
        if "participants" not in event.owner_fields and "evidence" not in event.owner_fields:
            for (entity_id, _role), participant in participants.items():
                row = await session.scalar(select(EventParticipant).where(
                    EventParticipant.event_id == event.id,
                    EventParticipant.event_id.in_(select(Event.id).where(Event.workspace_id == scope.workspace_id)),
                    EventParticipant.entity_id == entity_id,
                    EventParticipant.role == participant.role,
                ).with_for_update())
                if row is None:
                    row = EventParticipant(event_id=event.id, entity_id=entity_id, role=participant.role,
                                           metadata_json={}, origin="derived")
                    session.add(row)
                    await session.flush()
                rows = (await session.scalars(select(EventEvidence).where(
                    EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
                    EventEvidence.chunk_id.in_(participant.chunk_ids)
                ))).all()
                for evidence_row in rows:
                    await session.execute(insert(ParticipantEvidence).values(
                        participant_id=row.id, event_evidence_id=evidence_row.id
                    ).on_conflict_do_nothing(constraint="uq_timeline_participant_evidence"))
    await session.flush()
    proposals_json = [{
        "event_id": str(event.id), "candidate_hash": event.candidate_hash,
        "proposal": proposal.model_dump(mode="json"),
        "support": {"document_version_id": str(ready.document_version_id),
                    "chunk_ids": [str(item) for item in proposal.evidence_chunk_ids],
                    "membership_ids": [str(item.membership_id) for item in proposal.participants]},
        "refresh_conflicts": refresh_conflicts.get(str(event.candidate_hash), []),
    } for event, proposal in published]
    if not await finish_extraction_work(session, work_id, lease_owner, proposals_json, model, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled):
        return False
    for event, _proposal in published:
        await _schedule_temporal_event(session, event, ["extracted"], scope=scope,
            multi_workspace_enabled=multi_workspace_enabled)
    return True


async def summarize_source_events(
    session: AsyncSession, source_id: UUID, type_prefix: str, *, scope: Scope,
    multi_workspace_enabled: bool,
) -> tuple[dict[str, int], datetime | None]:
    """Count visible derived events per type for one source and return the latest observation time.

    Applies the same visibility rule as timeline listing (not deleted, backed by exact evidence)
    so counts never exceed what the Timeline can show. Read-only; admits the owner workspace scope before reading.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    escaped = type_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (await session.execute(
        select(Event.type, func.count(), func.max(Event.observed_at)).where(
            Event.workspace_id == scope.workspace_id, Event.source_id == source_id,
            Event.deleted_at.is_(None), Event.origin == "derived",
            Event.type.like(f"{escaped}%", escape="\\"),
            Event.id.in_(select(EventEvidence.event_id).where(
                EventEvidence.workspace_id == scope.workspace_id,
                EventEvidence.document_version_id.is_not(None), EventEvidence.chunk_id.is_not(None))),
        ).group_by(Event.type)
    )).all()
    return {row[0]: int(row[1]) for row in rows}, max((row[2] for row in rows), default=None)


async def publish_provider_event(
    session: AsyncSession, *, source_id: UUID, source_generation: int, document_id: UUID,
    document_version_id: UUID, chunk_ids: list[UUID], extraction_identity: str, record_key: str,
    event_type: str, title: str, summary: str | None, started_at: datetime, observed_at: datetime,
    metadata: dict[str, Any], participants: list[tuple[UUID, str]], scope: Scope,
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
        session, document_id=document_id, document_version_id=document_version_id,
        source_id=source_id, source_generation=source_generation, chunk_ids=chunk_ids,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if refs is None:
        return None
    evidence = await _read_evidence_closure(session, [(item.document_version_id, item.chunk_id) for item in refs],
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(evidence) != len(refs):
        return None
    canonical = await entities.get_entity_refs(session, [entity_id for entity_id, _ in participants], for_write=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    candidate_hash = sha256(record_key.encode("utf-8")).hexdigest()
    event = await session.scalar(select(Event).where(
        Event.workspace_id == scope.workspace_id, Event.extraction_identity == extraction_identity,
        Event.candidate_hash == candidate_hash,
    ).with_for_update())
    if event is not None and event.deleted_at is not None:
        return None
    changed = event is None
    if event is None:
        event = Event(
            workspace_id=scope.workspace_id, source_id=source_id, type=event_type, title=title[:300], summary=summary,
            confidence=1.0, metadata_json=metadata, origin="derived", date_precision="timed",
            started_at=started_at, observed_at=observed_at, extraction_identity=extraction_identity,
            candidate_hash=candidate_hash,
        )
        session.add(event)
        await session.flush()
    else:
        owners = set(event.owner_fields)
        desired = {"title": title[:300], "summary": summary, "type": event_type,
                   "started_at": started_at, "observed_at": observed_at, "metadata_json": metadata}
        updates = {name: value for name, value in desired.items()
                   if name not in owners and getattr(event, name) != value}
        for name, value in updates.items():
            setattr(event, name, value)
        if updates:
            event.revision += 1
            event.updated_at = datetime.now(UTC)
            changed = True
    for item in evidence:
        inserted = await session.execute(insert(EventEvidence).values(
            workspace_id=scope.workspace_id, event_id=event.id, source_id=item.source_id, document_id=item.document_id,
            document_version_id=item.document_version_id, chunk_id=item.chunk_id,
            version_number=item.version_number, source_generation=source_generation,
            extraction_identity=extraction_identity, candidate_hash=candidate_hash,
            confidence=1.0, extracted_at=datetime.now(UTC), observed_at=item.observed_at,
            title_snapshot=item.title, url_snapshot=item.canonical_url, evidence_metadata={},
            excerpt=item.excerpt, metadata_is_version_snapshot=item.metadata_is_version_snapshot,
        ).on_conflict_do_nothing(constraint="uq_timeline_event_evidence").returning(EventEvidence.id))
        changed = changed or inserted.first() is not None
    evidence_rows = list((await session.scalars(select(EventEvidence).where(
        EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
        EventEvidence.chunk_id.in_(chunk_ids),
        EventEvidence.document_version_id == document_version_id,
    ))).all())
    for ref, (_, role) in zip(canonical, participants):
        row = await session.scalar(select(EventParticipant).where(
            EventParticipant.event_id == event.id, EventParticipant.entity_id == ref.canonical_id,
            EventParticipant.role == role,
        ).with_for_update())
        if row is None:
            row = EventParticipant(event_id=event.id, entity_id=ref.canonical_id, role=role,
                                   metadata_json={}, origin="derived")
            session.add(row)
            await session.flush()
            changed = True
        for item in evidence_rows:
            await session.execute(insert(ParticipantEvidence).values(
                participant_id=row.id, event_evidence_id=item.id,
            ).on_conflict_do_nothing(constraint="uq_timeline_participant_evidence"))
    await session.flush()
    if changed:
        await _schedule_temporal_event(session, event, ["extracted"], scope=scope,
                                       multi_workspace_enabled=multi_workspace_enabled)
    return event.id


async def _schedule_temporal_event(
    session: AsyncSession, event: Event, fields: list[str], *, deleted: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Flush desired temporal history with the canonical event transaction, preserving exact evidence IDs."""
    from modules.knowledge.temporal import public as temporal
    rows = (await session.scalars(select(EventEvidence).where(
        EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id == event.id,
    ))).all()
    await temporal.schedule_canonical_change(
        session, kind="event", canonical_id=event.id, revision=event.revision, fields=fields,
        support=[(item.document_version_id, item.chunk_id) for item in rows
                 if item.document_version_id is not None and item.chunk_id is not None],
        origin=event.origin, deleted=deleted, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )


async def temporal_event_refs(
    session: AsyncSession, version_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[dict[str, Any]]:
    """Expose complete bounded canonical event/support identity for selected temporal versions; no private reads by callers."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if len(set(version_ids)) > 100:
        raise ValueError("Temporal event scope exceeds100 versions")
    rows = (await session.execute(select(Event, EventEvidence).join(
        EventEvidence, EventEvidence.event_id == Event.id,
    ).where(Event.workspace_id == scope.workspace_id, EventEvidence.workspace_id == scope.workspace_id,
            EventEvidence.document_version_id.in_(version_ids), Event.deleted_at.is_(None),
            ).order_by(Event.id, EventEvidence.id).limit(10001))).all()
    if len(rows) > 10000:
        raise ValueError("Temporal event support closure exceeds10000 references")
    return [{"event_id": event.id, "revision": event.revision, "origin": event.origin,
             "document_version_id": evidence.document_version_id, "chunk_id": evidence.chunk_id,
             "valid_from": event.valid_from, "valid_to": event.valid_to}
            for event, evidence in rows]


async def support_cleanup_ids(session: AsyncSession, *, document_id: UUID | None = None, source_id: UUID | None = None) -> tuple[list[UUID], list[UUID]]:
    """Return participant entity and all affected event IDs for upfront lock closure.

    Cleanup uses one source-scoped collection invalidation, so event identities
    are not truncated to the realtime per-batch limit.
    """
    statement = select(Event.id, EventParticipant.entity_id).join(EventEvidence, EventEvidence.event_id == Event.id).outerjoin(
        EventParticipant, EventParticipant.event_id == Event.id)
    if document_id is not None:
        statement = statement.where(EventEvidence.document_id == document_id)
    elif source_id is not None:
        statement = statement.where(EventEvidence.source_id == source_id)
    else:
        raise ValueError("specify one cleanup scope")
    rows = (await session.execute(statement)).all()
    return sorted({entity_id for _, entity_id in rows if entity_id is not None}, key=str), sorted({event_id for event_id, _ in rows}, key=str)


async def correction_event_ids(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope,
) -> list[UUID]:
    """Capture bounded event rows containing participants before correction locks are taken.

    The correction transaction already holds owner admission; this read only restricts rows to the workspace.
    """
    if not entity_ids:
        return []
    ids = list((await session.scalars(select(EventParticipant.event_id).join(
        Event, Event.id == EventParticipant.event_id,
    ).where(Event.workspace_id == scope.workspace_id,
        EventParticipant.entity_id.in_(entity_ids)
    ).distinct().order_by(EventParticipant.event_id).limit(201))).all())
    if len(ids) > 200:
        raise ValueError("Timeline correction closure exceeds 200 participant events")
    return sorted(set(ids), key=str)


async def lock_event_ids(
    session: AsyncSession, event_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Acquire captured event row locks in global UUID order after entity and relationship locks."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if event_ids:
        await session.execute(select(Event.id).where(Event.workspace_id == scope.workspace_id,
            Event.id.in_(sorted(set(event_ids), key=str))).order_by(Event.id).with_for_update())


async def revise_corrected_events(
    session: AsyncSession, event_ids: list[UUID], *, entity_id: UUID, scope: Scope,
    multi_workspace_enabled: bool,
) -> list[ReplayDraft]:
    """Bump each changed event once and return one entity-scoped collection invalidation.

    Callers must supply only events whose participant representation changed
    and must already hold their event locks after canonical entity locks.
    """
    changed_ids = sorted(set(event_ids), key=str)
    for event_id in changed_ids:
        event = await session.scalar(select(Event).where(
            Event.workspace_id == scope.workspace_id, Event.id == event_id,
        ))
        if event is not None:
            event.revision += 1
            event.updated_at = datetime.now(UTC)
            await _schedule_temporal_event(session, event, ["participants"], scope=scope,
                multi_workspace_enabled=multi_workspace_enabled)
    return [make_timeline_collection_change(entity_id=entity_id, scope=scope)] if changed_ids else []


async def apply_entity_merge(
    session: AsyncSession, *, source_id: UUID, target_id: UUID, event_ids: list[UUID], scope: Scope,
    multi_workspace_enabled: bool,
) -> list[UUID]:
    """Transfer links through prelocked events, coalesce roles with manual origin winning, and return changed IDs."""
    rows = list((await session.scalars(select(EventParticipant).join(
        Event, Event.id == EventParticipant.event_id,
    ).where(Event.workspace_id == scope.workspace_id,
        EventParticipant.event_id.in_(event_ids), EventParticipant.entity_id == source_id
    ).order_by(EventParticipant.event_id, EventParticipant.id).with_for_update())).all()) if event_ids else []
    changed_ids: set[UUID] = set()
    for row in rows:
        changed_ids.add(row.event_id)
        duplicate = await session.scalar(select(EventParticipant).where(
            EventParticipant.event_id == row.event_id, EventParticipant.entity_id == target_id,
            EventParticipant.role == row.role,
        ).with_for_update())
        if duplicate is None:
            row.entity_id = target_id
            continue
        evidence = list((await session.scalars(select(ParticipantEvidence).where(
            ParticipantEvidence.participant_id == row.id
        ))).all())
        existing_ids = set((await session.scalars(select(ParticipantEvidence.event_evidence_id).where(
            ParticipantEvidence.participant_id == duplicate.id
        ))).all())
        for support in evidence:
            if support.event_evidence_id in existing_ids:
                await session.delete(support)
            else:
                support.participant_id = duplicate.id
        if row.origin == "manual":
            duplicate.origin = "manual"
        await session.delete(row)
    return sorted(changed_ids, key=str)


async def apply_entity_split(session: AsyncSession, *, source_id: UUID, target_id: UUID,
                             event_ids: list[UUID], selected_pairs: set[tuple[UUID, UUID]], scope: Scope,
                             multi_workspace_enabled: bool) -> list[UUID]:
    """Move only selected exact-support derived links and return changed event IDs; retain manual links."""
    rows = list((await session.scalars(select(EventParticipant).join(
        Event, Event.id == EventParticipant.event_id,
    ).where(Event.workspace_id == scope.workspace_id,
        EventParticipant.event_id.in_(event_ids), EventParticipant.entity_id == source_id,
        EventParticipant.origin == "derived",
    ).order_by(EventParticipant.event_id, EventParticipant.id).with_for_update())).all()) if event_ids else []
    evidence_by_id = {item.id: item for item in (await session.scalars(select(EventEvidence).where(
        EventEvidence.workspace_id == scope.workspace_id, EventEvidence.event_id.in_(event_ids)
    ))).all()} if event_ids else {}
    changed_ids: set[UUID] = set()
    for row in rows:
        supports = list((await session.scalars(select(ParticipantEvidence).where(
            ParticipantEvidence.participant_id == row.id
        ))).all())
        selected_supports = [item for item in supports if item.event_evidence_id in evidence_by_id
            and evidence_by_id[item.event_evidence_id].document_version_id is not None
            and evidence_by_id[item.event_evidence_id].chunk_id is not None
            and (evidence_by_id[item.event_evidence_id].document_version_id,
                 evidence_by_id[item.event_evidence_id].chunk_id) in selected_pairs]
        if not selected_supports:
            continue
        changed_ids.add(row.event_id)
        duplicate = await session.scalar(select(EventParticipant).where(
            EventParticipant.event_id == row.event_id, EventParticipant.entity_id == target_id,
            EventParticipant.role == row.role,
        ).with_for_update())
        if duplicate is None:
            duplicate = EventParticipant(event_id=row.event_id, entity_id=target_id,
                role=row.role, metadata_json=row.metadata_json, origin="derived")
            session.add(duplicate)
            await session.flush()
        for support in selected_supports:
            support.participant_id = duplicate.id
        if len(selected_supports) == len(supports):
            await session.delete(row)
    return sorted(changed_ids, key=str)


async def remove_entity_participants(
    session: AsyncSession, entity_ids: list[UUID], *, scope: Scope, multi_workspace_enabled: bool,
) -> list[UUID]:
    """Remove participant links for terminally deleted entities and return their affected event IDs."""
    if entity_ids:
        event_ids = list((await session.scalars(select(EventParticipant.event_id).join(
            Event, Event.id == EventParticipant.event_id,
        ).where(Event.workspace_id == scope.workspace_id,
            EventParticipant.entity_id.in_(entity_ids),
        ).distinct())).all())
        await session.execute(delete(EventParticipant).where(
            EventParticipant.entity_id.in_(entity_ids), EventParticipant.event_id.in_(select(Event.id).where(
                Event.workspace_id == scope.workspace_id,
            )),
        ))
        return sorted(set(event_ids), key=str)
    return []


async def remove_document_support(session: AsyncSession, *, document_id: UUID, source_id: UUID) -> list[ReplayDraft]:
    """Remove one document's evidence and only participants whose exact support vanished; flush without commit."""
    evidence_ids = list((await session.scalars(select(EventEvidence.id).where(EventEvidence.document_id == document_id))).all())
    event_ids = list((await session.scalars(select(EventEvidence.event_id).where(EventEvidence.document_id == document_id))).all())
    participant_ids = list((await session.scalars(select(EventParticipant.id).join(
        ParticipantEvidence, ParticipantEvidence.participant_id == EventParticipant.id,
    ).where(ParticipantEvidence.event_evidence_id.in_(evidence_ids)))).all()) if evidence_ids else []
    await session.execute(delete(EventEvidence).where(EventEvidence.id.in_(evidence_ids)))
    orphaned = list((await session.scalars(select(EventParticipant.id).where(
        EventParticipant.id.in_(participant_ids), EventParticipant.origin == "derived",
        ~EventParticipant.id.in_(select(ParticipantEvidence.participant_id)),
    ))).all()) if participant_ids else []
    if orphaned:
        await session.execute(delete(EventParticipant).where(EventParticipant.id.in_(orphaned)))
    for event_id in sorted(set(event_ids), key=str):
        event = await session.get(Event, event_id, with_for_update=True)
        if event is not None:
            event.revision += 1
    await _hide_unsupported(session, event_ids)
    await session.flush()
    return [make_timeline_collection_change(source_id=source_id)] if event_ids else []


async def remove_source_support(session: AsyncSession, *, source_id: UUID) -> list[ReplayDraft]:
    """Remove one source's support while preserving unrelated evidence and owner-authored event fields."""
    event_ids = list((await session.scalars(select(EventEvidence.event_id).where(EventEvidence.source_id == source_id))).all())
    evidence_ids = list((await session.scalars(select(EventEvidence.id).where(EventEvidence.source_id == source_id))).all())
    participant_ids = list((await session.scalars(select(EventParticipant.id).join(
        ParticipantEvidence, ParticipantEvidence.participant_id == EventParticipant.id,
    ).where(ParticipantEvidence.event_evidence_id.in_(evidence_ids)))).all()) if evidence_ids else []
    await session.execute(delete(EventEvidence).where(EventEvidence.id.in_(evidence_ids)))
    orphaned = list((await session.scalars(select(EventParticipant.id).where(
        EventParticipant.id.in_(participant_ids), EventParticipant.origin == "derived",
        ~EventParticipant.id.in_(select(ParticipantEvidence.participant_id)),
    ))).all()) if participant_ids else []
    if orphaned:
        await session.execute(delete(EventParticipant).where(EventParticipant.id.in_(orphaned)))
    for event_id in sorted(set(event_ids), key=str):
        event = await session.get(Event, event_id, with_for_update=True)
        if event is not None:
            event.revision += 1
    await _hide_unsupported(session, event_ids)
    await session.flush()
    return [make_timeline_collection_change(source_id=source_id)] if event_ids else []


async def _hide_unsupported(session: AsyncSession, event_ids: list[UUID]) -> None:
    """Hide unsupported derived events and scrub every extracted field not explicitly owner-corrected."""
    for event_id in sorted(set(event_ids), key=str):
        event = await session.get(Event, event_id, with_for_update=True)
        if event is None or event.origin != "derived":
            continue
        remaining = await session.scalar(select(EventEvidence.id).where(EventEvidence.event_id == event_id).limit(1))
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
                event.occurrence_timezone = event.occurrence_timezone if "occurrence_timezone" in owners else None
            elif date_owner:
                # Keep only the date anchor required to represent an owner date correction.
                event.date_precision = "date"
                event.started_at = event.ended_at = None
                event.end_date = event.end_date if "end_date" in owners else None
                event.occurrence_timezone = event.occurrence_timezone if "occurrence_timezone" in owners else None
            elif "date_precision" in owners and event.date_precision == "unknown":
                event.date_precision = "unknown"
                event.started_at = event.ended_at = None
                event.occurred_date = event.end_date = None
                event.occurrence_timezone = None
            else:
                event.date_precision = "unknown"
                event.started_at = event.ended_at = None
                event.occurred_date = event.end_date = None
                event.occurrence_timezone = event.occurrence_timezone if "occurrence_timezone" in owners else None
            if not owners.intersection({"valid_from", "valid_to"}):
                event.valid_from = event.valid_to = None
            elif "valid_to" not in owners:
                event.valid_to = None
            # A corrected exclusive end still needs its current start boundary to satisfy the range shape.
            event.observed_at = event.created_at
            await session.execute(delete(EventParticipant).where(
                EventParticipant.event_id == event_id, EventParticipant.origin == "derived",
            ))


async def list_changed_events_after(
    session: AsyncSession, position: tuple[datetime, UUID] | None, limit: int = 100,
    *, scope: Scope, multi_workspace_enabled: bool,
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

