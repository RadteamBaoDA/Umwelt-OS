"""Timeline event reads, evidence, correlation signals and listing (private to the timeline package)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import desc, false, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces import public as workspaces  # noqa: F401  (patch target for fence tests)
from core.workspaces.schemas import (
    Scope,
)
from modules.knowledge.documents import public as documents
from modules.timeline._common import MAX_PAGE, _admit, _cursor_decode, _cursor_encode, day_window
from modules.timeline.models import (
    Event,
    EventEvidence,
    EventParticipant,
)
from modules.timeline.schemas import (
    BriefEventSupport,
    CorrelationSignalPage,
    CorrelationSignalRead,
    EventPage,
    EventRead,
    TimelinePage,
    TimelineQuery,
)


async def _event_read(
    session: AsyncSession,
    event: Event,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead:
    """Build an owner DTO from canonical rows and currently valid exact evidence."""
    participants = list(
        (
            await session.scalars(
                select(EventParticipant)
                .join(Event, Event.id == EventParticipant.event_id)
                .where(
                    Event.workspace_id == scope.workspace_id,
                    EventParticipant.event_id == event.id,
                )
                .order_by(EventParticipant.role, EventParticipant.entity_id)
            )
        ).all()
    )
    evidence_rows = list(
        (
            await session.scalars(
                select(EventEvidence)
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                )
                .order_by(EventEvidence.id)
            )
        ).all()
    )
    evidence: list[dict[str, Any]] = []
    for item in evidence_rows:
        if item.document_version_id is None or item.chunk_id is None:
            continue
        refs = await documents.read_evidence_refs(
            session,
            [(item.document_version_id, item.chunk_id)],
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        if not refs:
            continue
        ref = refs[0]
        evidence.append(
            {
                "source_id": ref.source_id,
                "document_id": ref.document_id,
                "document_version_id": ref.document_version_id,
                "version_number": ref.version_number,
                "chunk_id": ref.chunk_id,
                "observed_at": ref.observed_at,
                "title": item.title_snapshot if item.metadata_is_version_snapshot else ref.title,
                "canonical_url": item.url_snapshot
                if item.metadata_is_version_snapshot
                else ref.canonical_url,
                "metadata_is_version_snapshot": item.metadata_is_version_snapshot,
                "excerpt": ref.excerpt,
            }
        )
    return EventRead(
        id=event.id,
        source_id=event.source_id,
        type=event.type,
        subtype=event.subtype,
        title=event.title,
        summary=event.summary,
        importance_score=event.importance_score,
        confidence=event.confidence,
        metadata=event.metadata_json,
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
        participants=[
            {"entity_id": item.entity_id, "role": item.role, "metadata": item.metadata_json}
            for item in participants
        ],
        evidence=evidence,
    )


async def get_event(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventRead | None:
    """Read one visible event with detached participant and evidence projections."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(
        select(Event).where(
            Event.id == event_id,
            Event.workspace_id == scope.workspace_id,
            Event.deleted_at.is_(None),
        )
    )
    if event is None:
        return None
    if (
        event.origin == "derived"
        and await session.scalar(
            select(EventEvidence.id)
            .where(
                EventEvidence.workspace_id == scope.workspace_id,
                EventEvidence.event_id == event.id,
                EventEvidence.document_version_id.is_not(None),
                EventEvidence.chunk_id.is_not(None),
            )
            .limit(1)
        )
        is None
    ):
        return None
    return await _event_read(
        session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def list_event_evidence(
    session: AsyncSession,
    event_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[dict[str, Any]] | None:
    """Return exact live evidence references for a visible event, or None when absent."""
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    event = await session.scalar(
        select(Event.id).where(
            Event.id == event_id,
            Event.workspace_id == scope.workspace_id,
            Event.deleted_at.is_(None),
        )
    )
    if event is None:
        return None
    event_row = await session.scalar(
        select(Event).where(
            Event.id == event_id,
            Event.workspace_id == scope.workspace_id,
        )
    )
    # id was just selected above
    if event_row is None:
        raise RuntimeError("invariant violated: event_row must not be None")
    result = await _event_read(
        session, event_row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return result.evidence if result else None


async def lock_brief_events(
    session: AsyncSession,
    event_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
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
        select(Event.id)
        .where(
            Event.workspace_id == scope.workspace_id,
            Event.id.in_(ids),
            Event.deleted_at.is_(None),
        )
        .order_by(Event.id)
        .with_for_update(read=True)
    )
    return set(locked)


async def brief_event_support(
    session: AsyncSession,
    event_id: UUID,
    *,
    expected_title: str | None,
    expected_source_ids: list[str],
    lock_fact: bool = False,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> BriefEventSupport:
    """Return complete exact evidence or actual evidence-free manual origin for a brief fact.

    At most 100 support references are returned. The caller may request a shared
    event-row lock after it has acquired canonical source and document locks, so
    owner edits cannot race Dashboard model egress or final persistence.
    """
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    statement = select(Event).where(
        Event.id == event_id,
        Event.workspace_id == scope.workspace_id,
        Event.deleted_at.is_(None),
    )
    if lock_fact:
        statement = statement.with_for_update(read=True, of=Event)
    event = await session.scalar(statement.execution_options(populate_existing=True))
    unavailable = BriefEventSupport(
        event_id=event_id,
        title=expected_title or "",
        event_type="",
        origin="manual",
        source_ids=[],
        evidence=[],
        complete=False,
        independent=False,
    )
    if event is None or (expected_title is not None and event.title != expected_title):
        return unavailable
    evidence_rows = list(
        (
            await session.scalars(
                select(EventEvidence)
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                )
                .order_by(EventEvidence.id)
                .limit(101)
            )
        ).all()
    )
    if not evidence_rows:
        independent = event.origin == "manual" and not expected_source_ids
        return BriefEventSupport(
            event_id=event.id,
            title=event.title,
            event_type=event.type,
            origin=event.origin,
            source_ids=[],
            evidence=[],
            complete=independent,
            independent=independent,
        )
    if len(evidence_rows) > 100 or any(
        row.document_version_id is None or row.chunk_id is None for row in evidence_rows
    ):
        return unavailable.model_copy(update={"origin": event.origin})
    pairs = [
        (row.document_version_id, row.chunk_id)
        for row in evidence_rows
        if row.document_version_id is not None and row.chunk_id is not None
    ]  # None rows returned above
    refs = await documents.read_evidence_refs(
        session,
        pairs,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(refs) != len(pairs):
        return unavailable.model_copy(update={"origin": event.origin})
    support = [
        {
            "document_id": ref.document_id,
            "document_version_id": ref.document_version_id,
            "chunk_id": ref.chunk_id,
            "source_id": ref.source_id,
        }
        for ref in refs
    ]
    source_ids = sorted({ref["source_id"] for ref in support}, key=str)
    expected = sorted(set(expected_source_ids))
    if [str(value) for value in source_ids] != expected or len(
        {(ref["document_id"], ref["document_version_id"], ref["chunk_id"]) for ref in support}
    ) != len(support):
        return unavailable.model_copy(update={"origin": event.origin})
    return BriefEventSupport(
        event_id=event.id,
        title=event.title,
        event_type=event.type,
        origin=event.origin,
        source_ids=source_ids,
        evidence=support,
        complete=True,
        independent=False,
    )


async def list_correlation_signals(
    session: AsyncSession,
    *,
    domain: str,
    from_at: datetime,
    to_at: datetime,
    regions: list[str],
    source_ids: list[UUID],
    limit: int = 100,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> CorrelationSignalPage:
    """Project a bounded timed event slice into detached IDs after exact evidence checks.

    Region values must already be recorded under event metadata ``region``. Events
    without a live document-version/chunk reference are excluded. Military and
    escalation labels also require a recorded participant entity. No event title,
    summary, excerpt, or coordinate is returned to the correlation owner.
    """
    if (
        from_at.tzinfo is None
        or from_at.utcoffset() is None
        or to_at.tzinfo is None
        or to_at.utcoffset() is None
        or domain not in {"military", "economic", "disaster", "escalation"}
        or from_at >= to_at
        or not 1 <= limit <= 100
        or not 1 <= len(regions) <= 32
        or len(regions) != len(set(regions))
        or not 1 <= len(source_ids) <= 32
        or len(source_ids) != len(set(source_ids))
        or any(not region or len(region) > 80 or region != region.strip() for region in regions)
    ):
        raise ValueError("Invalid bounded correlation projection scope")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    selected_sources = set(source_ids)
    start, end = from_at.astimezone(UTC), to_at.astimezone(UTC)
    statement = (
        select(Event)
        .where(
            Event.workspace_id == scope.workspace_id,
            Event.deleted_at.is_(None),
            Event.date_precision == "timed",
            Event.started_at >= start,
            Event.started_at < end,
            Event.type == domain,
            Event.metadata_json["region"].as_string().in_(regions),
            Event.id.in_(
                select(EventEvidence.event_id).where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == Event.id,
                    EventEvidence.document_version_id.is_not(None),
                    EventEvidence.chunk_id.is_not(None),
                )
            ),
            # Coarse persisted-source admission bounds candidate work; live Document refs below
            # make the final decision and prevent stale EventEvidence.source_id from authorizing IDs.
            or_(
                Event.source_id.in_(selected_sources),
                Event.id.in_(
                    select(EventEvidence.event_id).where(
                        EventEvidence.workspace_id == scope.workspace_id,
                        EventEvidence.event_id == Event.id,
                        EventEvidence.source_id.in_(selected_sources),
                        EventEvidence.document_version_id.is_not(None),
                        EventEvidence.chunk_id.is_not(None),
                    )
                ),
            ),
        )
        .order_by(Event.started_at, Event.id)
        .limit(1_001)
    )
    rows = list((await session.scalars(statement)).all())
    signals: list[CorrelationSignalRead] = []
    for event in rows:
        if event.source_id is not None and event.source_id not in selected_sources:
            continue
        current = await _event_read(
            session, event, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        if not current.evidence or (
            event.type in {"military", "escalation"} and not current.participants
        ):
            continue
        live_evidence = {
            (item["document_version_id"], item["chunk_id"]): item
            for item in current.evidence
            if item["source_id"] in selected_sources
        }
        evidence_rows = (
            await session.scalars(
                select(EventEvidence)
                .where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == event.id,
                )
                .order_by(EventEvidence.id)
            )
        ).all()
        exact_rows = [
            item
            for item in evidence_rows
            if item.source_id in selected_sources
            and (item.document_version_id, item.chunk_id) in live_evidence
        ]
        if not exact_rows:
            continue
        exact_refs = [
            live_evidence[(item.document_version_id, item.chunk_id)] for item in exact_rows
        ]
        event_evidence_ids = [item.id for item in exact_rows]
        document_ids = sorted(
            {item["document_id"] for item in exact_refs if item["document_id"]}, key=str
        )
        document_version_ids = sorted(
            {item["document_version_id"] for item in exact_refs if item["document_version_id"]},
            key=str,
        )
        region = event.metadata_json.get("region")
        if not isinstance(region, str) or region not in regions:
            continue
        signals.append(
            CorrelationSignalRead(
                signal_id=event.id,
                event_id=event.id,
                event_type=event.type,
                region=region,
                observed_at=event.started_at,
                source_id=event.source_id,
                event_evidence_ids=event_evidence_ids[:100],
                document_ids=document_ids[:100],
                document_version_ids=document_version_ids[:100],
                chunk_ids=sorted({item.chunk_id for item in exact_rows if item.chunk_id}, key=str)[
                    :100
                ],
                omitted_event_evidence_ids=max(0, len(event_evidence_ids) - 100),
                omitted_document_ids=max(0, len(document_ids) - 100),
                omitted_document_version_ids=max(0, len(document_version_ids) - 100),
            )
        )
        if len(signals) > limit:
            break
    truncated = len(signals) > limit or len(rows) > 1_000
    return CorrelationSignalPage(items=signals[:limit], truncated=truncated)


async def _list_partition(  # noqa: C901, PLR0912
    session: AsyncSession,
    query: TimelineQuery,
    partition: int,
    key: Any,
    limit: int,
    *,
    scope: Scope,
) -> list[Event]:
    """Read a stable occurrence partition with shared visibility and type filters applied."""
    statement = (
        select(Event)
        .where(Event.workspace_id == scope.workspace_id, Event.deleted_at.is_(None))
        .where(
            (Event.origin == "manual")
            | Event.id.in_(
                select(EventEvidence.event_id).where(
                    EventEvidence.workspace_id == scope.workspace_id,
                    EventEvidence.event_id == Event.id,
                    EventEvidence.document_version_id.is_not(None),
                    EventEvidence.chunk_id.is_not(None),
                )
            )
        )
    )
    if query.source_id is not None:
        statement = statement.where(Event.source_id == query.source_id)
    if query.type is not None:
        # Escape SQL LIKE metacharacters so the user value remains a literal substring.
        type_pattern = query.type.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(Event.type.ilike(f"%{type_pattern}%", escape="\\"))
    if query.q is not None:
        # ponytail: ILIKE scan bounded by the page limit; add a pg_trgm index if event volume grows.
        q_pattern = query.q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        statement = statement.where(
            or_(
                Event.title.ilike(f"%{q_pattern}%", escape="\\"),
                Event.summary.ilike(f"%{q_pattern}%", escape="\\"),
            )
        )
    if partition == 0:
        statement = statement.where(Event.date_precision == "timed")
        if query.date_from is not None:
            start, _ = day_window(query.date_from, query.timezone)
            # validated together with date_from
            if query.date_to is None:
                raise RuntimeError("invariant violated: query.date_to must not be None")
            _, end = day_window(query.date_to - timedelta(days=1), query.timezone)
            statement = statement.where(Event.started_at >= start, Event.started_at < end)
        if query.entity_id is not None:
            statement = statement.where(
                Event.id.in_(
                    select(EventParticipant.event_id).where(
                        EventParticipant.entity_id == query.entity_id
                    )
                )
            )
        if key is not None:
            instant, identifier = key
            statement = statement.where(
                tuple_(Event.started_at, Event.id)
                < (datetime.fromisoformat(str(instant)), UUID(str(identifier)))
            )
        statement = statement.order_by(desc(Event.started_at), desc(Event.id))
    elif partition == 1:
        statement = statement.where(Event.date_precision == "date")
        if query.date_from is not None:
            statement = statement.where(
                Event.occurred_date >= query.date_from, Event.occurred_date < query.date_to
            )
        if query.entity_id is not None:
            statement = statement.where(
                Event.id.in_(
                    select(EventParticipant.event_id).where(
                        EventParticipant.entity_id == query.entity_id
                    )
                )
            )
        if key is not None:
            day, identifier = key
            statement = statement.where(
                tuple_(Event.occurred_date, Event.id)
                < (date.fromisoformat(str(day)), UUID(str(identifier)))
            )
        statement = statement.order_by(desc(Event.occurred_date), desc(Event.id))
    else:
        statement = statement.where(Event.date_precision == "unknown")
        if query.precision != "unknown" and query.date_from is not None:
            statement = statement.where(false())
        if query.entity_id is not None:
            statement = statement.where(
                Event.id.in_(
                    select(EventParticipant.event_id).where(
                        EventParticipant.entity_id == query.entity_id
                    )
                )
            )
        if key is not None:
            instant, identifier = key
            statement = statement.where(
                tuple_(Event.created_at, Event.id)
                < (datetime.fromisoformat(str(instant)), UUID(str(identifier)))
            )
        statement = statement.order_by(desc(Event.created_at), desc(Event.id))
    return list((await session.scalars(statement.limit(limit))).all())


async def list_events(
    session: AsyncSession,
    *,
    limit: int = 50,
    cursor: str | None = None,
    source_id: UUID | None = None,
    q: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> EventPage:
    """Return bounded events with stable timed/date/unknown cursor partitions.

    ``q`` is a literal title/summary substring that rides the same visibility filters and cursor fingerprint.
    """
    query = TimelineQuery(source_id=source_id, q=q)
    return await _list_page(
        session, query, limit, cursor, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def list_timeline(
    session: AsyncSession,
    query: TimelineQuery,
    *,
    limit: int = 50,
    cursor: str | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelinePage:
    """Return timeline pages in timed, date-only, then unknown partition order."""
    return await _list_page(
        session, query, limit, cursor, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def _list_page(
    session: AsyncSession,
    query: TimelineQuery,
    limit: int,
    cursor: str | None,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> TimelinePage:
    """Apply the shared finite cursor algorithm and detached DTO projection."""
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError("page size must be between 1 and 100")
    await _admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    filters = {**query.model_dump(mode="json"), "workspace_id": str(scope.workspace_id)}
    fingerprint = hashlib.sha256(
        json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    position = _cursor_decode(cursor, fingerprint)
    partition = int(position["p"])
    key = position["k"]
    items: list[Event] = []
    next_position: dict[str, object] | None = None
    while partition < 3 and len(items) < limit:
        if (
            query.precision == "timed"
            and partition != 0
            or query.precision == "date"
            and partition != 1
            or query.precision == "unknown"
            and partition != 2
        ):
            rows = []
        else:
            rows = await _list_partition(
                session, query, partition, key, limit - len(items) + 1, scope=scope
            )
        room = limit - len(items)
        items.extend(rows[:room])
        if len(rows) > room:
            last = rows[room - 1]
            value = (
                last.started_at
                if partition == 0
                else last.occurred_date
                if partition == 1
                else last.created_at
            )
            if value is None:
                raise RuntimeError("invariant violated: value must not be None")
            next_position = {
                "v": 1,
                "f": fingerprint,
                "p": partition,
                "k": [value.isoformat(), str(last.id)],
            }
            break
        partition += 1
        key = None
    if next_position is None and partition < 3 and len(items) == limit:
        next_position = {"v": 1, "f": fingerprint, "p": partition, "k": None}
    return TimelinePage(
        items=[
            await _event_read(
                session, row, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            for row in items
        ],
        next_cursor=_cursor_encode(next_position) if next_position else None,
    )


async def _evidence_rows(
    session: AsyncSession,
    pairs: list[tuple[UUID, UUID]],
    *,
    for_write: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> list[Any]:
    """Resolve exact version/chunk pairs through the documents owner boundary."""
    if not pairs:
        return []
    refs = await documents.read_evidence_refs(
        session,
        pairs,
        for_write=for_write,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if len(refs) != len(pairs):
        raise ValueError("event evidence is missing, inactive, or no longer authorized")
    return refs


async def _read_evidence_closure(
    session: AsyncSession,
    pairs: list[tuple[UUID, UUID]],
    *,
    scope: Scope,
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
        refs.extend(
            await documents.read_evidence_refs(
                session,
                pairs[offset : offset + 100],
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
        )
    return refs
