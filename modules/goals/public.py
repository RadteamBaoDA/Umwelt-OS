"""Owner-facing goal operations and stable task UoW integration contracts.

Milestones retain task IDs as history; completion follows the live owner task,
while accepted proposal manifests make retries deterministic and deletion-safe.
Cross-module task reads/writes go through modules.tasks.public DTO contracts.
"""

from __future__ import annotations

import hashlib
import json
import base64
import binascii
from datetime import UTC, datetime
from copy import deepcopy
from typing import Any, Sequence
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from modules.goals.models import Goal
from modules.goals.schemas import (
    GoalCreate, GoalExportFence, GoalExportPage, GoalExportValidation,
    GoalFilter, GoalPage, GoalRead, GoalUpdate, MilestoneSchema,
    PlanAcceptanceResult, PlanProposal, TaskProposal,
)
from modules.goals.seed import ensure_demo_goals

MAX_REVISION = 9_007_199_254_740_991
GOAL_EXPORT_PAGE_BYTES = 16_777_216


def _encode_goal_export_cursor(snapshot_at: datetime, created_at: datetime, identifier: UUID) -> str:
    """Bind a canonical goal keyset position to one immutable export cutoff."""
    raw = json.dumps(
        [1, "goals", snapshot_at.isoformat(), created_at.isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_goal_export_cursor(cursor: str) -> tuple[datetime, datetime, UUID]:
    """Reject noncanonical, oversized, cross-dataset, or future goal export cursors."""
    try:
        if len(cursor) > 512 or "=" in cursor:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 5 or values[:2] != [1, "goals"]:
            raise ValueError
        snapshot_at, created_at = datetime.fromisoformat(values[2]), datetime.fromisoformat(values[3])
        identifier = UUID(values[4])
        if (any(value.tzinfo is None or value.utcoffset() is None for value in (snapshot_at, created_at))
                or snapshot_at.isoformat() != values[2] or created_at.isoformat() != values[3]
                or created_at > snapshot_at or snapshot_at > datetime.now(UTC)
                or str(identifier) != values[4]
                or _encode_goal_export_cursor(snapshot_at, created_at, identifier) != cursor):
            raise ValueError
        return snapshot_at, created_at, identifier
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Goal export cursor is invalid") from exc


def _goal_export_scope(owner_id: int, snapshot_at: datetime) -> tuple[object, ...]:
    """Select one owner's stored goal revisions present at the export cutoff."""
    return Goal.owner_id == owner_id, Goal.created_at <= snapshot_at, Goal.updated_at <= snapshot_at


async def _goal_export_read(session: AsyncSession, row: Goal) -> GoalRead:
    """Reuse Goal's task/entity public projections while excluding internal replay manifests."""
    task_ids = _milestone_task_ids(row.milestones or [])
    task_states = await _task_rows_for_goal(session, row.owner_id, task_ids, row.id)
    result = await _reconcile_goal(row, task_states)
    return await _current_entity_projection(session, result)


async def export_page(
    session: AsyncSession, *, owner_id: int, record_kind: str, limit: int = 50, cursor: str | None = None,
) -> GoalExportPage:
    """Return a bounded owner page of stored goal fields with final-validation digests."""
    if owner_id != 1 or record_kind != "goals" or not 1 <= limit <= 100:
        raise ValueError("Goal export owner, kind or page limit is invalid")
    if cursor is None:
        snapshot_at, position = datetime.now(UTC), None
    else:
        snapshot_at, position_at, position_id = _decode_goal_export_cursor(cursor)
        position = (position_at, position_id)
    scope = _goal_export_scope(owner_id, snapshot_at)
    snapshot_count = int(await session.scalar(select(func.count()).select_from(Goal).where(*scope)) or 0)
    statement = select(Goal).where(*scope).execution_options(populate_existing=True)
    if position is not None:
        statement = statement.where(tuple_(Goal.created_at, Goal.id) > position)
    rows = list((await session.execute(statement.order_by(Goal.created_at, Goal.id).limit(limit + 1))).all())
    has_more, rows = len(rows) > limit, rows[:limit]
    items = [await _goal_export_read(session, row) for row in rows]
    encoded = [item.model_dump_json().encode("utf-8") for item in items]
    payload_bytes = 2 + sum(map(len, encoded)) + max(0, len(items) - 1)
    if payload_bytes > GOAL_EXPORT_PAGE_BYTES:
        raise HTTPException(status_code=413, detail="Goal export page exceeds its byte bound")
    fences = [GoalExportFence(
        id=row.id, created_at=row.created_at, updated_at=row.updated_at, revision=row.revision,
        content_digest=hashlib.sha256(raw).hexdigest(),
    ) for row, raw in zip(rows, encoded, strict=True)]
    return GoalExportPage(
        owner_id=owner_id, record_kind="goals", snapshot_at=snapshot_at,
        snapshot_count=snapshot_count, items=items, fences=fences, payload_bytes=payload_bytes,
        next_cursor=_encode_goal_export_cursor(snapshot_at, rows[-1].created_at, rows[-1].id)
        if has_more and rows else None,
    )


async def validate_export_fences(
    session: AsyncSession, *, owner_id: int, record_kind: str, snapshot_at: datetime,
    expected_snapshot_count: int, fences: list[GoalExportFence],
) -> GoalExportValidation:
    """Recheck each goal digest and the fixed-cutoff owner inventory before publication."""
    if owner_id != 1 or record_kind != "goals" or len(fences) > 100:
        raise ValueError("Goal export validation input is invalid")
    observed = int(await session.scalar(
        select(func.count()).select_from(Goal).where(*_goal_export_scope(owner_id, snapshot_at))
    ) or 0)
    if observed != expected_snapshot_count:
        return GoalExportValidation(valid=False, reason="snapshot_count_changed", observed_snapshot_count=observed)
    for fence in fences:
        row = await session.scalar(select(Goal).where(
            Goal.id == fence.id, *_goal_export_scope(owner_id, snapshot_at),
        ).execution_options(populate_existing=True))
        if row is None:
            return GoalExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
        item = await _goal_export_read(session, row)
        if (item.created_at != fence.created_at or item.updated_at != fence.updated_at
                or item.revision != fence.revision
                or hashlib.sha256(item.model_dump_json().encode("utf-8")).hexdigest() != fence.content_digest):
            return GoalExportValidation(valid=False, reason="record_changed", observed_snapshot_count=observed)
    return GoalExportValidation(valid=True, reason="valid", observed_snapshot_count=observed)


class GoalConflict(Exception):
    """Represent a revision collision, invalid link, or idempotency mismatch."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Initialize the public conflict code and optional current revision."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class GoalMissing(Exception):
    """Represent a goal absent from the authenticated owner's scope."""


def _calculate_progress(milestones: list[dict[str, Any]]) -> float:
    """Calculate automatic progress from milestone state, including task links."""
    if not milestones:
        return 0.0
    return round(sum(1 for item in milestones if item.get("completed") is True) * 100.0 / len(milestones), 1)


def _milestone_task_ids(milestones: list[dict[str, Any]]) -> list[UUID]:
    """Extract unique, parseable task links from persisted milestone JSON."""
    ids = []
    for item in milestones:
        try:
            if item.get("task_id"):
                ids.append(UUID(str(item["task_id"])))
        except (ValueError, TypeError):
            continue
    return list(dict.fromkeys(ids))


def _accepted_task_ids(manifests: list[dict[str, Any]]) -> list[UUID]:
    """Extract unique task identities retained by accepted-plan replay manifests."""
    ids = []
    for manifest in manifests:
        for value in manifest.get("task_ids", []):
            try:
                ids.append(UUID(str(value)))
            except (ValueError, TypeError):
                continue
    return list(dict.fromkeys(ids))


def _to_goal_read(goal: Goal) -> GoalRead:
    """Project a goal row to its public DTO, skipping malformed legacy milestone entries."""
    milestones = []
    for value in goal.milestones or []:
        try:
            milestones.append(MilestoneSchema.model_validate(value))
        except (ValueError, TypeError):
            continue
    return GoalRead(
        id=goal.id, owner_id=goal.owner_id, title=goal.title, description=goal.description,
        desired_outcome=goal.desired_outcome, deadline=goal.deadline, progress=goal.progress,
        manual_progress=goal.manual_progress, status=goal.status, milestones=milestones,
        entity_ids=[UUID(str(value)) for value in deepcopy(goal.entity_ids or [])],
        revision=goal.revision, created_at=goal.created_at, updated_at=goal.updated_at,
    )


async def _current_entity_projection(session: AsyncSession, result: GoalRead) -> GoalRead:
    """Project only current canonical entities through the entity owner's read contract."""
    if not result.entity_ids:
        return result
    from modules.knowledge.entities import public as entities

    visible = []
    for entity_id in result.entity_ids:
        try:
            refs = await entities.get_entity_refs(session, [entity_id])
        except LookupError:
            continue
        visible.append(refs[0].canonical_id)
    result.entity_ids = list(dict.fromkeys(visible))
    return result


async def _lock_goal(session: AsyncSession, owner_id: int, goal_id: UUID) -> Goal:
    """Acquire an owner-scoped goal row lock used as the first domain lock."""
    row = await session.scalar(select(Goal).where(
        Goal.id == goal_id, Goal.owner_id == owner_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise GoalMissing
    return row


async def require_active_goal_link(session: AsyncSession, owner_id: int, goal_id: UUID) -> None:
    """Reject task links to missing, foreign, or non-active goals."""
    goal = await session.scalar(select(Goal.id).where(
        Goal.id == goal_id, Goal.owner_id == owner_id, Goal.status == "active",
    ))
    if goal is None:
        raise ValueError("goal_id must identify an active goal owned by this account")


async def lock_owned_goal_links(
    session: AsyncSession, owner_id: int, goal_ids: Sequence[UUID]
) -> None:
    """Lock at most two existing owner goals in stable ID order before task locks."""
    if len(goal_ids) > 2 or len(set(goal_ids)) != len(goal_ids):
        raise ValueError("A task mutation may link at most two distinct goals")
    if not goal_ids:
        return
    rows = list((await session.scalars(select(Goal).where(
        Goal.id.in_(goal_ids), Goal.owner_id == owner_id,
    ).order_by(Goal.id).with_for_update())).all())
    if {row.id for row in rows} != set(goal_ids):
        raise ValueError("Goal link is missing or foreign")


async def _task_rows_for_goal(
    session: AsyncSession, owner_id: int, task_ids: Sequence[UUID], goal_id: UUID
) -> dict[UUID, bool]:
    """Read completion states through the task owner's bounded detached DTO contract."""
    from modules.tasks import public as tasks

    return await tasks.linked_task_completion(session, owner_id, task_ids, goal_id)


async def _reconcile_goal(goal: Goal, task_states: dict[UUID, bool]) -> GoalRead:
    """Build a detached progress projection from current task state without mutating the row."""
    result = _to_goal_read(goal)
    for milestone in result.milestones:
        if milestone.task_id is not None:
            milestone.completed = task_states.get(milestone.task_id, False)
    if not result.manual_progress:
        result.progress = _calculate_progress([
            item.model_dump(mode="json") for item in result.milestones
        ])
    return result


async def reconcile_task_milestone(
    session: AsyncSession, owner_id: int, task_id: UUID, locked_goal_ids: Sequence[UUID],
) -> None:
    """Persist final task linkage/completion into each affected already-locked goal once."""
    from modules.tasks import public as tasks

    if len(locked_goal_ids) > 2:
        raise ValueError("Task progress reconciliation is bounded to two goals")
    for goal_id in locked_goal_ids:
        goal = await session.get(Goal, goal_id)
        if goal is None or goal.owner_id != owner_id:
            continue
        changed = False
        # JSONB values are nested mutable structures; detached copies make change detection reliable.
        milestones = deepcopy(goal.milestones or [])
        state = await tasks.linked_task_completion(session, owner_id, [task_id], goal_id)
        effective = state.get(task_id, False)
        for milestone in milestones:
            if milestone.get("task_id") == str(task_id):
                value = bool(effective)
                if milestone.get("completed") is not value:
                    milestone["completed"] = value
                    changed = True
        progress = goal.progress if goal.manual_progress else _calculate_progress(milestones)
        changed = changed or goal.progress != progress
        if not changed:
            continue
        if goal.revision >= MAX_REVISION:
            raise GoalConflict("revision_exhausted", "Goal revision counter has reached the maximum safe integer limit", goal.revision)
        goal.milestones = milestones
        goal.progress = progress
        goal.revision += 1


async def create_goal(session: AsyncSession, owner_id: int, payload: GoalCreate) -> GoalRead:
    """Create an owner goal with manual or milestone-derived progress."""
    if any(item.task_id is not None for item in payload.milestones):
        raise ValueError("A new goal cannot link tasks before it exists")
    if len(payload.milestones) + len(payload.entity_ids) > 100:
        raise ValueError("A goal may contain at most 100 milestone and entity references")
    from modules.tasks import public as tasks

    await tasks.validate_entity_references_for_write(session, payload.entity_ids)
    values = [item.model_dump(mode="json") for item in payload.milestones]
    goal = Goal(
        owner_id=owner_id, title=payload.title, description=payload.description,
        desired_outcome=payload.desired_outcome, deadline=payload.deadline,
        progress=payload.progress if payload.manual_progress and payload.progress is not None else (0.0 if payload.manual_progress else _calculate_progress(values)),
        manual_progress=payload.manual_progress, status=payload.status,
        milestones=values, entity_ids=[str(value) for value in payload.entity_ids],
        accepted_proposals=[], revision=1,
    )
    session.add(goal)
    await session.commit()
    await session.refresh(goal)
    return await _current_entity_projection(session, _to_goal_read(goal))


async def update_goal(session: AsyncSession, owner_id: int, goal_id: UUID, payload: GoalUpdate) -> GoalRead:
    """Patch an owner goal with revision checks and null-as-clear semantics.

    Distinct task IDs are sent to the bounded task-owner queries, while every milestone
    remains in the stored list and contributes independently to progress.
    """
    goal = await _lock_goal(session, owner_id, goal_id)
    if payload.expected_revision != goal.revision:
        raise GoalConflict("stale_revision", f"Expected revision {payload.expected_revision} but goal is at {goal.revision}", goal.revision)
    if goal.revision >= MAX_REVISION:
        raise GoalConflict("revision_exhausted", "Goal revision counter has reached the maximum safe integer limit", goal.revision)
    fields = payload.model_fields_set
    if "title" in fields and payload.title is None:
        raise ValueError("title cannot be cleared")
    if "status" in fields and payload.status is None:
        raise ValueError("status cannot be cleared")
    next_milestones = None
    if "milestones" in fields:
        next_milestones = [item.model_dump(mode="json") for item in (payload.milestones or [])]
        # Several milestone entries may validly point at one task; query that identity once.
        linked_ids = list(dict.fromkeys(
            UUID(str(item["task_id"])) for item in next_milestones if item.get("task_id")
        ))
        prior_links = set(_milestone_task_ids(goal.milestones or []))
        from modules.tasks import public as tasks

        await tasks.validate_linked_tasks(
            session, owner_id, linked_ids, goal.id,
            allow_deleted_ids=list(set(linked_ids) & prior_links),
        )
        task_states = await tasks.linked_task_completion(session, owner_id, linked_ids, goal.id)
        for item in next_milestones:
            if item.get("task_id"):
                item["completed"] = task_states.get(UUID(str(item["task_id"])), False)
    current_milestones = next_milestones if next_milestones is not None else goal.milestones or []
    current_entities = (payload.entity_ids or []) if "entity_ids" in fields else [
        UUID(str(value)) for value in goal.entity_ids or []
    ]
    linked_task_ids = set(_milestone_task_ids(current_milestones))
    linked_task_ids.update(_accepted_task_ids(goal.accepted_proposals or []))
    if len(current_milestones) + len(linked_task_ids) + len(current_entities) > 100:
        raise ValueError("A goal may contain at most 100 milestone, task, and entity references")
    if "entity_ids" in fields:
        from modules.tasks import public as tasks

        await tasks.validate_entity_references_for_write(session, current_entities)
    for name in ("title", "description", "desired_outcome", "deadline", "status"):
        if name in fields:
            setattr(goal, name, getattr(payload, name))
    if "manual_progress" in fields:
        goal.manual_progress = bool(payload.manual_progress)
    if next_milestones is not None:
        goal.milestones = next_milestones
    if "entity_ids" in fields:
        goal.entity_ids = [str(value) for value in current_entities]
    if goal.manual_progress:
        if "progress" in fields:
            goal.progress = payload.progress if payload.progress is not None else 0.0
    else:
        goal.progress = _calculate_progress(goal.milestones or [])
    goal.revision += 1
    await session.commit()
    await session.refresh(goal)
    return await _current_entity_projection(session, _to_goal_read(goal))


async def get_goal(session: AsyncSession, owner_id: int, goal_id: UUID) -> GoalRead:
    """Fetch a goal inside the authenticated owner's scope."""
    goal = await session.scalar(select(Goal).where(Goal.id == goal_id, Goal.owner_id == owner_id))
    if goal is None:
        raise GoalMissing
    task_ids = _milestone_task_ids(goal.milestones or [])
    result = await _reconcile_goal(
        goal, await _task_rows_for_goal(session, owner_id, task_ids, goal_id)
    )
    return await _current_entity_projection(session, result)


async def list_goals(session: AsyncSession, owner_id: int, filter: GoalFilter) -> GoalPage:
    """Return a bounded cursor page of owner goals, with linked progress refreshed."""
    statement = select(Goal).where(Goal.owner_id == owner_id)
    if filter.status is not None: statement = statement.where(Goal.status == filter.status)
    if filter.q and filter.q.strip():
        term = f"%{filter.q.strip()}%"
        statement = statement.where(Goal.title.ilike(term) | Goal.description.ilike(term) | Goal.desired_outcome.ilike(term))
    statement = statement.order_by(Goal.created_at.desc(), Goal.id.desc())
    if filter.cursor:
        cursor_dt, cursor_id = decode_cursor(filter.cursor)
        statement = statement.where((Goal.created_at < cursor_dt) | ((Goal.created_at == cursor_dt) & (Goal.id < cursor_id)))
    rows = list((await session.scalars(statement.limit(filter.limit + 1))).all())
    more, rows = len(rows) > filter.limit, rows[:filter.limit]
    results = []
    for goal in rows:
        task_ids = _milestone_task_ids(goal.milestones or [])
        result = await _reconcile_goal(
            goal, await _task_rows_for_goal(session, owner_id, task_ids, goal.id)
        )
        results.append(await _current_entity_projection(session, result))
    cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if more and rows else None
    return GoalPage(items=results, next_cursor=cursor)


async def delete_goal(session: AsyncSession, owner_id: int, goal_id: UUID, expected_revision: int) -> None:
    """Delete a revision-fenced goal after its task owner contract detaches linked tasks."""
    goal = await _lock_goal(session, owner_id, goal_id)
    if goal.revision != expected_revision:
        raise GoalConflict("stale_revision", f"Expected revision {expected_revision} but goal is at {goal.revision}", goal.revision)
    if goal.revision >= MAX_REVISION:
        raise GoalConflict("revision_exhausted", "Goal revision counter has reached the maximum safe integer limit", goal.revision)
    from modules.tasks import public as tasks

    try:
        await tasks.detach_goal_tasks(session, owner_id, goal_id)
    except tasks.TaskConflict as exc:
        raise GoalConflict(exc.code, str(exc), exc.current_revision) from exc
    await session.delete(goal)
    await session.commit()


def _proposal_hash(proposal: PlanProposal) -> str:
    """Hash immutable accepted proposal content without random default milestone IDs."""
    # Exclude generated milestone IDs only; explicitly supplied IDs remain content-bound.
    value = proposal.model_dump(mode="json", exclude={"proposal_id", "expected_revision"})
    for index, item in enumerate(proposal.milestones):
        if "id" not in item.model_fields_set:
            value["milestones"][index].pop("id", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


async def accept_plan(session: AsyncSession, owner_id: int, goal_id: UUID, proposal: PlanProposal) -> PlanAcceptanceResult:
    """Atomically accept a content-bound proposal once and replay its stored task manifest."""
    goal = await _lock_goal(session, owner_id, goal_id)
    fingerprint = _proposal_hash(proposal)
    manifest = list(goal.accepted_proposals or [])
    previous = next((item for item in manifest if item.get("proposal_id") == proposal.proposal_id), None)
    if previous is not None:
        if previous.get("content_hash") != fingerprint:
            raise GoalConflict("idempotency_key_reused", "proposal_id was already accepted with different content", goal.revision)
        from modules.tasks import public as tasks

        live, deleted = await tasks.accepted_task_results(session, owner_id, [UUID(item) for item in previous.get("task_ids", [])])
        return PlanAcceptanceResult(
            goal=await _current_entity_projection(session, _to_goal_read(goal)),
            created_tasks=live, deleted_task_ids=deleted,
            accepted_goal_revision=previous["accepted_goal_revision"],
            accepted_milestone_ids=[UUID(item) for item in previous.get("milestone_ids", [])],
            already_accepted=True,
        )
    if goal.status != "active":
        raise GoalConflict("goal_inactive", "Plan proposals can only be accepted by an active goal", goal.revision)
    if proposal.expected_revision != goal.revision:
        raise GoalConflict("stale_revision", f"Expected revision {proposal.expected_revision} but goal is at {goal.revision}", goal.revision)
    if goal.revision >= MAX_REVISION:
        raise GoalConflict("revision_exhausted", "Goal revision counter has reached the maximum safe integer limit", goal.revision)
    if len(manifest) >= 1000:
        raise ValueError("A goal may retain at most 1000 accepted proposal manifests")
    existing_milestones = goal.milestones or []
    existing_entities = goal.entity_ids or []
    existing_task_ids = set(_milestone_task_ids(existing_milestones))
    existing_task_ids.update(_accepted_task_ids(manifest))
    proposal_entity_ids = [
        identifier for task in proposal.tasks for identifier in task.entity_ids
    ]
    total_references = (
        len(existing_milestones) + len(existing_task_ids)
        + len(existing_entities) + len(proposal.milestones) + len(proposal.tasks)
    )
    if total_references > 100:
        raise ValueError("A goal may contain at most 100 milestone, task, and entity references")
    if any(item.task_index is not None and item.task_index >= len(proposal.tasks) for item in proposal.milestones):
        raise ValueError("milestone task_index must refer to a task in this proposal")
    proposal_milestone_ids = [
        item.id or uuid5(goal.id, f"accepted:{proposal.proposal_id}:milestone:{index}")
        for index, item in enumerate(proposal.milestones)
    ]
    if len(set(proposal_milestone_ids)) != len(proposal_milestone_ids) or any(str(item) in {str(value.get("id")) for value in goal.milestones or []} for item in proposal_milestone_ids):
        raise GoalConflict("milestone_id_conflict", "Proposal milestone ID is already present on this goal", goal.revision)

    from modules.tasks import public as tasks

    # Lock the full bounded entity union before task inserts; per-task locking could
    # otherwise deadlock against a concurrent proposal accepted in reverse task order.
    entity_lock_ids = sorted(set(proposal_entity_ids), key=str)
    if len(entity_lock_ids) > 100:
        raise ValueError("A proposal may reference at most 100 unique entities")
    await tasks.validate_entity_references_for_write(session, entity_lock_ids)

    milestone_values = deepcopy(existing_milestones)
    task_ids: list[UUID] = []
    created = []
    # The goal row is already locked; task UoW validates sorted entity references before inserts.
    for index, task in enumerate(proposal.tasks):
        payload = TaskProposal.model_validate(task.model_dump())
        from modules.tasks.schemas import TaskCreate

        created.append(await tasks.create_task_in_uow(session, owner_id, TaskCreate(
            title=payload.title, description=payload.description, status=payload.status,
            due_date=payload.due_date, due_at=payload.due_at, goal_id=goal.id,
            entity_ids=payload.entity_ids,
        ), idempotency_key=(goal.id, proposal.proposal_id, index)))
        task_ids.append(created[-1].id)
    for index, item in enumerate(proposal.milestones):
        milestone_id = item.id or uuid5(goal.id, f"accepted:{proposal.proposal_id}:milestone:{index}")
        task_id = task_ids[item.task_index] if item.task_index is not None and item.task_index < len(task_ids) else None
        milestone_values.append(MilestoneSchema(
            id=milestone_id, title=item.title, completed=bool(task_id and proposal.tasks[item.task_index].status == "done"),
            due_date=item.due_date, order=item.order, task_id=task_id,
        ).model_dump(mode="json"))
    goal.milestones = milestone_values
    if not goal.manual_progress:
        goal.progress = _calculate_progress(milestone_values)
    goal.revision += 1
    manifest.append({
        "proposal_id": proposal.proposal_id, "content_hash": fingerprint,
        "task_ids": [str(item) for item in task_ids],
        "milestone_ids": [str(item) for item in proposal_milestone_ids],
        "accepted_goal_revision": goal.revision,
    })
    goal.accepted_proposals = manifest
    await session.commit()
    await session.refresh(goal)
    return PlanAcceptanceResult(
        goal=await _current_entity_projection(session, _to_goal_read(goal)),
        created_tasks=created, deleted_task_ids=[],
        accepted_goal_revision=goal.revision,
        accepted_milestone_ids=proposal_milestone_ids,
        already_accepted=False,
    )


class GoalService:
    """Bind goal public operations to an asynchronous SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by the service facade."""
        self.session = session

    async def create_goal(self, owner_id: int, payload: GoalCreate) -> GoalRead:
        """Create an owner goal through the public contract."""
        return await create_goal(self.session, owner_id, payload)

    async def update_goal(self, owner_id: int, goal_id: UUID, payload: GoalUpdate) -> GoalRead:
        """Patch an owner goal through the public contract."""
        return await update_goal(self.session, owner_id, goal_id, payload)

    async def get_goal(self, owner_id: int, goal_id: UUID) -> GoalRead:
        """Read an owner goal through the public contract."""
        return await get_goal(self.session, owner_id, goal_id)

    async def list_goals(self, owner_id: int, filter: GoalFilter) -> GoalPage:
        """List goals through the public bounded query contract."""
        return await list_goals(self.session, owner_id, filter)

    async def delete_goal(self, owner_id: int, goal_id: UUID, expected_revision: int) -> None:
        """Delete a revision-fenced goal through the public contract."""
        await delete_goal(self.session, owner_id, goal_id, expected_revision)

    async def accept_plan(self, owner_id: int, goal_id: UUID, proposal: PlanProposal) -> PlanAcceptanceResult:
        """Accept or replay one owner-approved proposal through the public UoW."""
        return await accept_plan(self.session, owner_id, goal_id, proposal)


async def list_deadlines_within(
    session: AsyncSession, owner_id: int, lead_days: int, limit: int = 100,
) -> list[tuple[UUID, str, str, int, float]]:
    """Active goals whose deadline is within ``lead_days`` ahead (or up to one day past), bounded.

    Returns ``(goal_id, status, deadline_marker, days_until_deadline, progress)`` for the due sweep.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    today = datetime.now(UTC).date()
    rows = (await session.scalars(select(Goal).where(
        Goal.owner_id == owner_id, Goal.status == "active", Goal.deadline.is_not(None),
        Goal.deadline.between(today - timedelta(days=1), today + timedelta(days=lead_days)),
    ).order_by(Goal.deadline, Goal.id).limit(limit))).all()
    return [(g.id, g.status, g.deadline.isoformat(), (g.deadline - today).days, float(g.progress)) for g in rows]
