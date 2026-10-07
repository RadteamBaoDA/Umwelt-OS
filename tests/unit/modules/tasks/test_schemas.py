"""Unit tests for task module schemas and due date exclusivity validation.

Covers:
- TaskCreate validation: title, status, timezone awareness of due_at, unique entity_ids
- Due date vs due instant mutual exclusivity in TaskCreate and TaskUpdate
- TaskUpdate optimistic revision concurrency, non-empty patch requirement, non-nullable fields
- TaskRead projection schema
- TaskFilter parameter validation, view literals, and filter instant awareness
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.tasks.schemas import (
    TaskCreate,
    TaskFilter,
    TaskRead,
    TaskUpdate,
)


class TestTaskCreateSchemas:
    """Tests for TaskCreate payload validation and constraints."""

    def test_task_create_minimal(self) -> None:
        """Verify minimal valid task creation with default status."""
        task = TaskCreate(title="Review pull request")
        assert task.title == "Review pull request"
        assert task.status == "inbox"
        assert task.due_date is None
        assert task.due_at is None
        assert task.entity_ids == []

    def test_task_create_with_due_date_only(self) -> None:
        """Verify TaskCreate with date-only deadline."""
        d = date(2026, 10, 15)
        task = TaskCreate(title="Tax filing deadline", due_date=d)
        assert task.due_date == d
        assert task.due_at is None

    def test_task_create_with_aware_due_at_only(self) -> None:
        """Verify TaskCreate with timezone-aware datetime deadline."""
        dt = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
        task = TaskCreate(title="Dentist appointment", due_at=dt)
        assert task.due_at == dt
        assert task.due_date is None

    def test_task_create_due_at_naive_rejected(self) -> None:
        """Naive datetime without timezone must be rejected."""
        naive_dt = datetime(2026, 10, 15, 14, 30)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="due_at must include a timezone offset"):
            TaskCreate(title="Test", due_at=naive_dt)

    def test_task_create_due_fields_exclusive_rejected(self) -> None:
        """Providing BOTH due_date and due_at simultaneously must be rejected."""
        d = date(2026, 10, 15)
        dt = datetime(2026, 10, 15, 14, 30, tzinfo=UTC)
        with pytest.raises(ValidationError, match="due_date and due_at are mutually exclusive"):
            TaskCreate(title="Test", due_date=d, due_at=dt)

    def test_task_create_duplicate_entity_ids_rejected(self) -> None:
        """Duplicate entity references must be rejected."""
        ent_id = uuid4()
        with pytest.raises(ValidationError, match="entity_ids must contain at most 100 unique IDs"):
            TaskCreate(title="Test", entity_ids=[ent_id, ent_id])

    def test_task_create_entity_ids_bound(self) -> None:
        """Entity IDs exceeding 100 must be rejected."""
        ids = [uuid4() for _ in range(101)]
        with pytest.raises(ValidationError, match="entity_ids must contain at most 100 unique IDs"):
            TaskCreate(title="Test", entity_ids=ids)

    def test_task_create_invalid_status(self) -> None:
        """Invalid task status must be rejected."""
        with pytest.raises(ValidationError):
            TaskCreate(title="Test", status="archived")  # type: ignore[arg-type]


class TestTaskUpdateSchemas:
    """Tests for TaskUpdate patch validation, revision checking, and exclusivity."""

    def test_task_update_valid(self) -> None:
        """Verify valid partial update."""
        patch = TaskUpdate(
            title="Updated title",
            status="in_progress",
            expected_revision=1,
        )
        assert patch.title == "Updated title"
        assert patch.status == "in_progress"
        assert patch.expected_revision == 1

    def test_task_update_empty_mutation_rejected(self) -> None:
        """TaskUpdate containing only expected_revision with no mutations must be rejected."""
        with pytest.raises(ValidationError, match="task patch must contain at least one mutation field"):
            TaskUpdate(expected_revision=1)

    def test_task_update_clearing_title_rejected(self) -> None:
        """Attempting to clear mandatory title to None must be rejected."""
        with pytest.raises(ValidationError, match="title cannot be cleared"):
            TaskUpdate(title=None, expected_revision=1)

    def test_task_update_clearing_status_rejected(self) -> None:
        """Attempting to clear status to None must be rejected."""
        with pytest.raises(ValidationError, match="status cannot be cleared"):
            TaskUpdate(status=None, expected_revision=1)

    def test_task_update_due_exclusivity(self) -> None:
        """TaskUpdate cannot provide both non-null due_date and due_at simultaneously."""
        d = date(2026, 10, 20)
        dt = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
        with pytest.raises(ValidationError, match="due_date and due_at are mutually exclusive"):
            TaskUpdate(due_date=d, due_at=dt, expected_revision=1)

    def test_task_update_completed_at_aware(self) -> None:
        """completed_at must be timezone-aware."""
        naive = datetime(2026, 10, 5, 12, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="instant timestamps must include a timezone offset"):
            TaskUpdate(completed_at=naive, expected_revision=1)


class TestTaskReadAndFilter:
    """Tests for TaskRead projection and TaskFilter query schemas."""

    def test_task_read_schema(self) -> None:
        """Verify TaskRead model construction."""
        task_id = uuid4()
        now = datetime.now(UTC)
        read = TaskRead(
            id=task_id,
            owner_id=1,
            title="Read book chapter",
            description="Chapter 4: Consistency models",
            status="todo",
            due_date=date(2026, 10, 10),
            due_at=None,
            completed_at=None,
            goal_id=None,
            entity_ids=[],
            revision=1,
            created_at=now,
            updated_at=now,
        )
        assert read.id == task_id
        assert read.status == "todo"
        assert read.revision == 1

    def test_task_filter_defaults(self) -> None:
        """Verify TaskFilter defaults (limit 50, Asia/Ho_Chi_Minh timezone)."""
        filt = TaskFilter()
        assert filt.limit == 50
        assert filt.timezone == "Asia/Ho_Chi_Minh"
        assert filt.view is None

    def test_task_filter_views_valid(self) -> None:
        """Verify allowed TaskView literals: inbox, today, upcoming, blocked, completed, all."""
        for v in ["inbox", "today", "upcoming", "blocked", "completed", "all"]:
            filt = TaskFilter(view=v)  # type: ignore[arg-type]
            assert filt.view == v

    def test_task_filter_instant_naive_rejected(self) -> None:
        """Filter instants (due_at_from / due_at_to) must be timezone-aware."""
        naive = datetime(2026, 10, 1, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="instant filters must include a timezone offset"):
            TaskFilter(due_at_from=naive)
