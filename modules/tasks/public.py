"""Owner-facing task query, mutation, and unit-of-work contracts.

Callers use these DTO contracts instead of importing task persistence models.
Writes are owner-scoped, revision fenced, and keep goal progress reconciliation
inside the caller's transaction when a task is linked to a milestone. The
explicit demo seed export is flush-only and leaves commit ownership to its
document-seed coordinator.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Sequence
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.pagination import decode_cursor, encode_cursor
from modules.tasks.models import Task
from modules.tasks.schemas import TaskCreate, TaskFilter, TaskPage, TaskRead, TaskUpdate
from modules.tasks.seed import ensure_demo_tasks

MAX_REVISION = 9_007_199_254_740_991


class TaskConflict(Exception):
    """Represent a revision mismatch, exhausted revision, or invalid task state."""

    def __init__(self, code: str, message: str, current_revision: int | None = None) -> None:
        """Initialize a stable conflict code and optional current revision."""
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class TaskMissing(Exception):
    """Represent a task that is absent, deleted, or owned by another account."""


def _to_task_read(task: Task) -> TaskRead:
    """Project a live task row into its detached public DTO."""
    return TaskRead.model_validate(task)


async def _current_entity_projection(session: AsyncSession, result: TaskRead) -> TaskRead:
    """Keep only entity links that still resolve through the entity owner's read API."""
    from modules.knowledge.entities import public as entities

    visible = []
    for entity_id in result.entity_ids:
        try:
            visible.append((await entities.get_entity_refs(session, [entity_id]))[0].canonical_id)
        except LookupError:
            continue
    result.entity_ids = list(dict.fromkeys(visible))
    return result


async def _validate_references(
    session: AsyncSession, owner_id: int, entity_ids: Sequence[UUID], goal_id: UUID | None
) -> None:
    """Validate linked entity and goal IDs through their owner public contracts."""
    if entity_ids:
        await validate_entity_references_for_write(session, entity_ids)
    if goal_id is not None:
        from modules.goals import public as goals

        await goals.require_active_goal_link(session, owner_id, goal_id)


async def validate_entity_references_for_write(
    session: AsyncSession, entity_ids: Sequence[UUID]
) -> None:
    """Resolve and lock a bounded entity-reference set through its owning public contract.

    The entity owner orders canonical row locks. Missing, deleted, redirected, or
    otherwise unwriteable references become a stable request validation error.
    """
    if not entity_ids:
        return
    from modules.knowledge.entities import public as entities

    try:
        await entities.get_entity_refs(session, list(entity_ids), for_write=True)
    except LookupError as exc:
        raise ValueError("entity_ids must identify current canonical entities") from exc


async def _locked_goal_ids(
    session: AsyncSession, owner_id: int, goal_ids: Sequence[UUID | None]
) -> list[UUID]:
    """Lock linked goals in UUID order before task rows to avoid lock inversion."""
    # Goal rows precede task rows in every task mutation to give moves and deletes one lock order.
    ids = sorted({item for item in goal_ids if item is not None}, key=str)
    if ids:
        from modules.goals import public as goals

        await goals.lock_owned_goal_links(session, owner_id, ids)
    return ids


async def create_task_in_uow(
    session: AsyncSession, owner_id: int, payload: TaskCreate,
    *, idempotency_key: tuple[UUID, str, int] | None = None,
) -> TaskRead:
    """Add a server-identified task without committing the caller's transaction.

    Goal acceptance uses this owner UoW contract while holding the goal fence;
    an optional server-composed scope/key/index deterministically generates identity.
    """
    await _validate_references(session, owner_id, payload.entity_ids, payload.goal_id)
    if idempotency_key is not None:
        goal_id, key, position = idempotency_key
        if payload.goal_id != goal_id or not key or len(key) > 128 or not 0 <= position < 100:
            raise ValueError("Invalid task materialization idempotency scope")
        task_id = uuid5(goal_id, f"accepted:{key}:task:{position}")
        if await session.scalar(select(Task.id).where(Task.id == task_id)) is not None:
            raise TaskConflict("task_id_conflict", "Server-generated accepted task ID is already occupied")
    else:
        task_id = uuid4()
    task = Task(
        id=task_id, owner_id=owner_id, title=payload.title,
        description=payload.description, status=payload.status,
        due_date=payload.due_date, due_at=payload.due_at,
        completed_at=datetime.now(UTC) if payload.status == "done" else None,
        goal_id=payload.goal_id, entity_ids=[str(value) for value in payload.entity_ids],
        revision=1,
    )
    session.add(task)
    await session.flush()
    return _to_task_read(task)


async def create_task(session: AsyncSession, owner_id: int, payload: TaskCreate) -> TaskRead:
    """Create one owner task and commit its validated references atomically."""
    goals = await _locked_goal_ids(session, owner_id, [payload.goal_id])
    result = await create_task_in_uow(session, owner_id, payload)
    if goals:
        from modules.goals import public as goal_public

        await goal_public.reconcile_task_milestone(session, owner_id, result.id, goals)
    await session.commit()
    return await get_task(session, owner_id, result.id)


async def update_task(
    session: AsyncSession, owner_id: int, task_id: UUID, payload: TaskUpdate
) -> TaskRead:
    """Apply a revision-fenced patch with coherent due and completion transitions."""
    locator = await session.scalar(select(Task.goal_id).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    ))
    if locator is None and not await session.scalar(select(Task.id).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    )):
        raise TaskMissing
    requested_goal = payload.goal_id if "goal_id" in payload.model_fields_set else locator
    locked_goals = await _locked_goal_ids(session, owner_id, [locator, requested_goal])
    task = await session.scalar(select(Task).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    ).with_for_update().execution_options(populate_existing=True))
    if task is None:
        raise TaskMissing
    if task.goal_id != locator:
        raise TaskConflict("stale_revision", "Task link changed during update", task.revision)
    if task.revision != payload.expected_revision:
        raise TaskConflict("stale_revision", f"Expected revision {payload.expected_revision} but task is at {task.revision}", task.revision)
    if task.revision >= MAX_REVISION:
        raise TaskConflict("revision_exhausted", "Task revision counter has reached the maximum safe integer limit", task.revision)

    fields = payload.model_fields_set
    next_goal_id = payload.goal_id if "goal_id" in fields else task.goal_id
    next_entity_ids = payload.entity_ids if "entity_ids" in fields else [UUID(value) for value in task.entity_ids]
    next_due_date = payload.due_date if "due_date" in fields else task.due_date
    next_due_at = payload.due_at if "due_at" in fields else task.due_at
    next_status = payload.status if "status" in fields and payload.status is not None else task.status
    if next_due_date is not None and next_due_at is not None:
        raise ValueError("due_date and due_at are mutually exclusive")
    if "goal_id" in fields and next_goal_id is not None:
        from modules.goals import public as goals

        await goals.require_active_goal_link(session, owner_id, next_goal_id)
    if "entity_ids" in fields and next_entity_ids:
        # Updates acquire goal locks, then the task row, then sorted entity-owner fences.
        await validate_entity_references_for_write(session, next_entity_ids)
    if "title" in fields and payload.title is None:
        raise ValueError("title cannot be cleared")
    if "completed_at" in fields and payload.completed_at is not None and next_status != "done":
        raise ValueError("completed_at is only valid for a done task")
    if "title" in fields and payload.title is not None: task.title = payload.title
    if "description" in fields: task.description = payload.description
    task.status = next_status
    task.goal_id = next_goal_id
    if "entity_ids" in fields: task.entity_ids = [str(item) for item in (next_entity_ids or [])]
    task.due_date = next_due_date
    task.due_at = next_due_at

    if task.status == "done":
        task.completed_at = payload.completed_at if "completed_at" in fields and payload.completed_at is not None else (task.completed_at or datetime.now(UTC))
    else:
        task.completed_at = None
    task.revision += 1
    await session.flush()
    try:
        if locked_goals:
            from modules.goals import public as goal_public

            # Recompute each affected goal from the task's final persisted linkage/status once.
            await goal_public.reconcile_task_milestone(session, owner_id, task_id, locked_goals)
    except goal_public.GoalConflict as exc:
        await session.rollback()
        raise TaskConflict(exc.code, str(exc), exc.current_revision) from exc
    await session.commit()
    return await get_task(session, owner_id, task_id)


async def get_task(session: AsyncSession, owner_id: int, task_id: UUID) -> TaskRead:
    """Return one non-deleted task visible to its owner."""
    task = await session.scalar(select(Task).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    ))
    if task is None: raise TaskMissing
    return await _current_entity_projection(session, _to_task_read(task))


async def list_tasks(session: AsyncSession, owner_id: int, filter: TaskFilter) -> TaskPage:
    """Return an owner-fenced, bounded task page using date-only and local-instant semantics."""
    statement = select(Task).where(Task.owner_id == owner_id, Task.deleted_at.is_(None))
    try:
        zone = ZoneInfo(filter.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    local_day = datetime.now(zone).date()
    # Local-midnight boundaries are computed independently, so DST days can be 23 or 25 hours.
    start = datetime.combine(local_day, time.min, zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    if filter.view == "inbox": statement = statement.where(Task.status == "inbox")
    elif filter.view == "today": statement = statement.where(((Task.due_date == local_day) | ((Task.due_at >= start) & (Task.due_at < end))), Task.status.notin_(("done", "cancelled")))
    elif filter.view == "upcoming": statement = statement.where(((Task.due_date > local_day) | (Task.due_at >= end)), Task.status.notin_(("done", "cancelled")))
    elif filter.view == "blocked": statement = statement.where(Task.status == "blocked")
    elif filter.view == "completed":
        # Cancelled work remains discoverable under All/status=cancelled, not Completed.
        statement = statement.where(Task.status == "done")
    if filter.status is not None: statement = statement.where(Task.status == filter.status)
    if filter.goal_id is not None: statement = statement.where(Task.goal_id == filter.goal_id)
    if filter.entity_id is not None: statement = statement.where(Task.entity_ids.contains([str(filter.entity_id)]))
    if filter.due_date_from is not None: statement = statement.where(Task.due_date >= filter.due_date_from)
    if filter.due_date_to is not None: statement = statement.where(Task.due_date <= filter.due_date_to)
    if filter.due_at_from is not None: statement = statement.where(Task.due_at >= filter.due_at_from)
    if filter.due_at_to is not None: statement = statement.where(Task.due_at <= filter.due_at_to)
    if filter.q and filter.q.strip():
        term = f"%{filter.q.strip()}%"
        statement = statement.where(Task.title.ilike(term) | Task.description.ilike(term))
    statement = statement.order_by(Task.created_at.desc(), Task.id.desc())
    if filter.cursor:
        cursor_dt, cursor_id = decode_cursor(filter.cursor)
        statement = statement.where((Task.created_at < cursor_dt) | ((Task.created_at == cursor_dt) & (Task.id < cursor_id)))
    rows = list((await session.scalars(statement.limit(filter.limit + 1))).all())
    more, rows = len(rows) > filter.limit, rows[:filter.limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if more and rows else None
    items = []
    for row in rows:
        items.append(await _current_entity_projection(session, _to_task_read(row)))
    return TaskPage(items=items, next_cursor=next_cursor)


async def delete_task(session: AsyncSession, owner_id: int, task_id: UUID, expected_revision: int) -> None:
    """Soft-delete a revision-fenced task while retaining accepted-plan identity."""
    goal_id = await session.scalar(select(Task.goal_id).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    ))
    if goal_id is None and not await session.scalar(select(Task.id).where(Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None))):
        raise TaskMissing
    locked_goals = await _locked_goal_ids(session, owner_id, [goal_id])
    task = await session.scalar(select(Task).where(
        Task.id == task_id, Task.owner_id == owner_id, Task.deleted_at.is_(None),
    ).with_for_update())
    if task is None: raise TaskMissing
    if task.revision != expected_revision:
        raise TaskConflict("stale_revision", f"Expected revision {expected_revision} but task is at {task.revision}", task.revision)
    if task.revision >= MAX_REVISION:
        raise TaskConflict("revision_exhausted", "Task revision counter has reached the maximum safe integer limit", task.revision)
    # Preserve status/completed_at as lifecycle history; deleted_at hides it from live views.
    # Keeping the pair intact also satisfies the table's status/completion invariant.
    task.deleted_at = datetime.now(UTC)
    task.revision += 1
    if goal_id is not None:
        from modules.goals import public as goal_public

        try:
            await goal_public.reconcile_task_milestone(session, owner_id, task_id, locked_goals)
        except goal_public.GoalConflict as exc:
            await session.rollback()
            raise TaskConflict(exc.code, str(exc), exc.current_revision) from exc
    await session.commit()


async def accepted_task_results(
    session: AsyncSession, owner_id: int, task_ids: Sequence[UUID]
) -> tuple[list[TaskRead], list[UUID]]:
    """Return live accepted tasks plus tombstoned IDs without recreating either."""
    if len(task_ids) > 100: raise ValueError("Accepted task manifest exceeds 100 entries")
    rows = list((await session.scalars(select(Task).where(
        Task.owner_id == owner_id, Task.id.in_(task_ids), Task.deleted_at.is_(None),
    ).order_by(Task.id))).all()) if task_ids else []
    live = {row.id: _to_task_read(row) for row in rows}
    visible = [await _current_entity_projection(session, live[item]) for item in task_ids if item in live]
    return visible, [item for item in task_ids if item not in live]


async def linked_task_completion(
    session: AsyncSession, owner_id: int, task_ids: Sequence[UUID], goal_id: UUID | None
) -> dict[UUID, bool]:
    """Return bounded live task completion states under owner and optional goal fences."""
    if len(task_ids) > 100 or len(set(task_ids)) != len(task_ids):
        raise ValueError("Linked task lookup requires at most 100 unique IDs")
    if not task_ids:
        return {}
    statement = select(Task).where(
        Task.id.in_(task_ids), Task.owner_id == owner_id, Task.deleted_at.is_(None),
    )
    if goal_id is not None:
        statement = statement.where(Task.goal_id == goal_id)
    rows = list((await session.scalars(statement)).all())
    return {row.id: row.status == "done" and row.completed_at is not None for row in rows}


async def validate_linked_tasks(
    session: AsyncSession, owner_id: int, task_ids: Sequence[UUID], goal_id: UUID,
    *, allow_deleted_ids: Sequence[UUID] = (),
) -> None:
    """Validate milestone links, preserving only pre-existing tombstoned IDs as history."""
    if len(task_ids) > 100 or len(set(task_ids)) != len(task_ids):
        raise ValueError("A goal may link at most 100 unique milestone tasks")
    if not set(allow_deleted_ids).issubset(task_ids):
        raise ValueError("Historical task links must also be present in the milestone list")
    if not task_ids:
        return
    rows = list((await session.scalars(select(Task).where(
        Task.id.in_(task_ids), Task.owner_id == owner_id, Task.goal_id == goal_id,
    ))).all())
    by_id = {row.id: row for row in rows}
    if set(by_id) != set(task_ids) or any(
        row.deleted_at is not None and row.id not in allow_deleted_ids
        for row in rows
    ):
        raise ValueError("Milestone task links must identify live tasks or existing tombstone history for this goal")


async def detach_goal_tasks(session: AsyncSession, owner_id: int, goal_id: UUID) -> None:
    """Detach all owner task rows from a deleting goal and advance each task revision."""
    rows = list((await session.scalars(select(Task).where(
        Task.owner_id == owner_id, Task.goal_id == goal_id,
    ).order_by(Task.id).with_for_update())).all())
    if any(row.revision >= MAX_REVISION for row in rows):
        raise TaskConflict("revision_exhausted", "A linked task revision counter is exhausted")
    for row in rows:
        row.goal_id = None
        row.revision += 1


class TaskService:
    """Bind the public task operations to one asynchronous database session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used for delegated task operations."""
        self.session = session

    async def create_task(self, owner_id: int, payload: TaskCreate) -> TaskRead:
        """Create a task through the owner-scoped public contract."""
        return await create_task(self.session, owner_id, payload)

    async def update_task(self, owner_id: int, task_id: UUID, payload: TaskUpdate) -> TaskRead:
        """Update a task through the required-revision public contract."""
        return await update_task(self.session, owner_id, task_id, payload)

    async def get_task(self, owner_id: int, task_id: UUID) -> TaskRead:
        """Read a task through the owner-scoped public contract."""
        return await get_task(self.session, owner_id, task_id)

    async def list_tasks(self, owner_id: int, filter: TaskFilter) -> TaskPage:
        """List tasks through the bounded public query contract."""
        return await list_tasks(self.session, owner_id, filter)

    async def delete_task(self, owner_id: int, task_id: UUID, expected_revision: int) -> None:
        """Soft-delete a task through the revision-fenced public contract."""
        await delete_task(self.session, owner_id, task_id, expected_revision)
