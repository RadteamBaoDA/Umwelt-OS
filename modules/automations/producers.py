"""Automations-owned trigger producers: bounded cursor sweeps through each owner module's public API.

The durable ``event_outbox`` keeps its single consumer; nothing here marks or consumes it. Each
sweep keeps a durable ``(ts, id)`` cursor in ``automation_cursors`` that advances in the same
transaction as the inbox rows it produced, reads at most ``BATCH`` items per tick, and relies on
the inbox/run dedupe keys, so a crash or overlapping pass only repeats absorbed work. A cursor
starts at "now" the first time it is seen: history is never replayed into new rules. While no live
rule uses a trigger type its cursor just follows the clock.

``task_due`` and ``goal_deadline`` are time-derived (due-window sweeps) and plan runs per rule,
because each rule has its own lead time. Task events carry the creating run's origin and depth
when a task came from an automation (``origin_for_reference``), so depth 5 and the self-origin
guard hold across chains. Documents, entities and timeline events expose no creator reference, so
those events enter as root events.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.automations import execution
from modules.automations.models import AutomationCursor
from modules.goals import public as goals
from modules.ingestion import public as ingestion
from modules.knowledge.documents import public as documents
from modules.knowledge.entities import public as entities
from modules.sources import public as sources
from modules.tasks import public as tasks
from modules.timeline import public as timeline

logger = logging.getLogger(__name__)
OWNER_ID = 1
BATCH = 100
# Server-side `now()` defaults/onupdate stamp the *transaction start*, not the commit. A writer that
# started at T and commits after a sweep has moved past T would otherwise be skipped forever, so every
# read re-scans from `cursor.ts - CURSOR_LAG`; the inbox/run dedupe keys absorb the overlap. Must exceed
# the longest producer write transaction.
CURSOR_LAG = timedelta(seconds=30)
DUE_GRACE = timedelta(days=1)  # a due moment missed by more than a day is not announced late
_Reader = Callable[
    [AsyncSession, tuple[datetime, UUID] | None, int],
    Awaitable[list[tuple[datetime, UUID, str, dict[str, Any] | None]]],
]
_CURSORS: dict[str, _Reader] = {
    "new_document": ingestion.list_ready_events_after,
    "connector_sync_result": ingestion.list_terminal_runs_after,
    "new_event": timeline.list_changed_events_after,
    "entity_changed": entities.list_changed_entities_after,
}


async def _cursor_sweep(session: AsyncSession, name: str, reader: _Reader, now: datetime) -> int:
    """Move one cursor forward by at most ``BATCH`` items, offering each to the trigger inbox.

    Reads start ``CURSOR_LAG`` before the cursor (see the constant). When a lagged page is full and
    shows nothing newer than the cursor (a backlog denser than the window), the read falls back to the
    exact cursor so progress is guaranteed. A reader item with a None payload (malformed or
    soft-deleted) is never offered but still moves the cursor. Document identity is passed separately
    from the unchanged public condition whitelist. New-document Source IDs are proved and prelocked
    in UUID order before the mutable cursor row; the bounded page is re-read and revalidated under
    those fences before enqueue.
    """
    cursor = await session.get(AutomationCursor, name)
    if cursor is None:
        session.add(AutomationCursor(name=name, ts=now, item_id=None))
        return 0
    if not await execution.live_rules(session, name):
        locked = await session.scalar(
            select(AutomationCursor).where(AutomationCursor.name == name).with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is not None:
            locked.ts, locked.item_id = now, None
        return 0
    current = (cursor.ts, cursor.item_id or UUID(int=0))
    items = await reader(session, (cursor.ts - CURSOR_LAG, UUID(int=0)), BATCH)
    if len(items) == BATCH and (items[-1][0], items[-1][1]) <= current:
        items = await reader(session, current, BATCH)
    # Publication fences precede the mutable cursor row. Prelock every source represented by this
    # bounded detached page in UUID order, then revalidate each event during enrichment/admission.
    if name == "new_document":
        source_ids: set[UUID] = set()
        for _ts, item_id, key, payload in items:
            if payload is None:
                continue
            try:
                event_id = UUID(key)
            except (KeyError, TypeError, ValueError):
                continue
            if str(event_id) != key or event_id != item_id:
                continue
            proof = await ingestion.resolve_ready_event_provenance(session, event_id)
            if proof is not None:
                source_ids.add(proof.source_id)
        for source_id in sorted(source_ids, key=str):
            await sources.lock_retained_evidence_source(session, source_id)
    cursor = await session.scalar(
        select(AutomationCursor).where(AutomationCursor.name == name).with_for_update()
        .execution_options(populate_existing=True)
    )
    if cursor is None or (cursor.ts, cursor.item_id or UUID(int=0)) != current:
        return 0
    # Re-read under the acquired fences; events may have changed while the ordered Source set was locked.
    items = await reader(session, (cursor.ts - CURSOR_LAG, UUID(int=0)), BATCH)
    if len(items) == BATCH and (items[-1][0], items[-1][1]) <= current:
        items = await reader(session, current, BATCH)
    if name == "new_document":
        reread_sources: set[UUID] = set()
        for _ts, item_id, key, payload in items:
            if payload is None:
                continue
            try:
                event_id = UUID(key)
            except (KeyError, TypeError, ValueError):
                continue
            if str(event_id) != key or event_id != item_id:
                continue
            proof = await ingestion.resolve_ready_event_provenance(session, event_id)
            if proof is not None:
                reread_sources.add(proof.source_id)
        if not reread_sources.issubset(source_ids):
            # Acquiring a newly discovered lower UUID here could invert the already-held lock order.
            return 0
    last = (items[-1][0], items[-1][1]) if items else None  # advance past dropped items too
    offerable = [item for item in items if item[3] is not None]
    document_items: list[tuple[datetime, UUID, str, dict[str, Any], UUID, UUID]] | None = None
    if name == "new_document":
        document_items = await _enrich_documents(session, offerable)
    offered = 0
    if document_items is not None:
        for _ts, _item_id, key, payload, document_id, version_id in document_items:
            try:
                offered += await execution.enqueue_trigger(
                    session, OWNER_ID, name, key, payload,
                    document_id=document_id, document_version_id=version_id,
                )
            except ValueError:
                pass  # an item with an undeclared shape is skipped, never retried forever
    else:
        for _ts, _item_id, key, payload in offerable:
            try:
                offered += await execution.enqueue_trigger(session, OWNER_ID, name, key, payload)
            except ValueError:
                pass  # an item with an undeclared shape is skipped, never retried forever
    if last is not None and last > current:
        cursor.ts, cursor.item_id = last
    return offered


async def _enrich_documents(
    session: AsyncSession, items: list[tuple[datetime, UUID, str, dict[str, Any]]],
) -> list[tuple[datetime, UUID, str, dict[str, Any], UUID, UUID]]:
    """Add title, mime type and source type (metadata only, no content) to ready-document events.

    The emitted title is current Document metadata, not a historical version snapshot; exact event
    version identity travels separately from condition fields. Invalid or deleted evidence is dropped.
    """
    ids = [UUID(p["document_id"]) for _, _, _, p in items]
    meta = await documents.document_metadata(session, ids) if ids else {}
    source_types: dict[str, str] = {}
    enriched = []
    for ts, item_id, key, payload in items:
        try:
            document_id, event_id = UUID(payload["document_id"]), UUID(key)
            version_id = UUID(payload["document_version_id"])
        except (KeyError, ValueError, TypeError):
            continue
        if str(event_id) != key or event_id != item_id:
            continue
        proof = await ingestion.resolve_ready_event_provenance(session, event_id)
        if (
            proof is None or proof.document_id != document_id or proof.document_version_id != version_id
            or proof.source_id != UUID(payload["source_id"])
        ):
            continue
        found = meta.get(document_id)
        if found is None:
            continue
        title, mime_type = found
        source_id = payload["source_id"]
        if source_id not in source_types:
            source = await sources.get_source(session, UUID(source_id))
            source_types[source_id] = source.type if source is not None else ""
        body: dict[str, Any] = {"source_id": source_id, "title": title}
        if mime_type:
            body["mime_type"] = mime_type
        if source_types[source_id]:
            body["source_type"] = source_types[source_id]
        enriched.append((ts, item_id, key, body, document_id, version_id))
    return enriched


async def _due_sweep(session: AsyncSession) -> int:
    """Plan runs for tasks and goals whose due moment is inside each rule's lead window.

    Identity ``(rule, revision, kind:id:due:lead)`` makes the sweep idempotent; moving a due date
    produces a new key and fires again, which is intended.
    """
    created = 0
    for rev in await execution.live_rules(session, "task_due"):
        lead = rev.trigger.get("lead_minutes", 0)
        for task_id, status, due, hours, goal_id in await tasks.list_due_within(
            session, OWNER_ID, timedelta(minutes=lead), DUE_GRACE, BATCH,
        ):
            key = f"task:{task_id}:{due}:{lead}"
            if await execution.run_exists(session, rev, key):
                continue
            origin = await execution.origin_for_reference(session, f"task:{task_id}")
            created += await execution.plan_run(
                session, owner_id=OWNER_ID, rev=rev, trigger_type="task_due", trigger_key=key,
                trigger_event_id=f"{task_id}:{due}", slot=None,
                payload={
                    "status": status, "hours_until_due": hours, "created_by_automation": origin is not None,
                    **({"goal_id": str(goal_id)} if goal_id is not None else {}),
                },
                depth=origin[2] + 1 if origin else 1,
                origin_automation_id=origin[0] if origin else None, origin_run_id=origin[1] if origin else None,
            ) is not None
    for rev in await execution.live_rules(session, "goal_deadline"):
        lead_days = rev.trigger.get("lead_days", 0)
        for goal_id, status, deadline, days, progress in await goals.list_deadlines_within(
            session, OWNER_ID, lead_days, BATCH,
        ):
            key = f"goal:{goal_id}:{deadline}:{lead_days}"
            if await execution.run_exists(session, rev, key):
                continue
            created += await execution.plan_run(
                session, owner_id=OWNER_ID, rev=rev, trigger_type="goal_deadline", trigger_key=key,
                trigger_event_id=f"{goal_id}:{deadline}", slot=None,
                payload={"goal_id": str(goal_id), "status": status, "days_until_deadline": days, "progress": progress},
                depth=1, origin_automation_id=None, origin_run_id=None,
            ) is not None
    return created


async def sweep(factory: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Run every producer once, each in its own transaction, and return rows created.

    Isolation: a failing sweep (poisoned item, first-cursor insert race) rolls back only itself; the
    others, the schedule tick and dispatch still run, and the failed one retries next tick. A failure is
    logged as a warning (sweep name and exception class only) and the pass continues.
    """
    now = now or datetime.now(UTC)
    total = 0
    sweeps: list[tuple[str, Any]] = [
        (name, (lambda s, n=name, r=reader: _cursor_sweep(s, n, r, now))) for name, reader in _CURSORS.items()
    ] + [("due", _due_sweep)]
    for name, run in sweeps:
        try:
            async with factory() as session:
                total += await run(session)
                await session.commit()
        except Exception as exc:
            # Name and class only: never the exception text, which could carry item content.
            logger.warning("automation producer sweep %s failed (%s)", name, type(exc).__name__)
    return total
