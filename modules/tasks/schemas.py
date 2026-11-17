"""Validated Pydantic DTOs and filters for task management operations."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskStatus = Literal["inbox", "todo", "in_progress", "blocked", "done", "cancelled"]
TaskView = Literal["inbox", "today", "upcoming", "blocked", "completed", "all"]


class TaskCreate(BaseModel):
    """Payload contract for creating an owner task."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    status: TaskStatus = "inbox"
    due_date: date | None = None
    due_at: datetime | None = None
    goal_id: UUID | None = None
    entity_ids: list[UUID] = Field(default_factory=list)

    @field_validator("due_at")
    @classmethod
    def due_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive instants so stored deadlines always identify a real instant."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("due_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def due_fields_are_exclusive(self) -> "TaskCreate":
        """Require exactly one due representation when a task has a deadline."""
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        if len(set(self.entity_ids)) != len(self.entity_ids) or len(self.entity_ids) > 100:
            raise ValueError("entity_ids must contain at most 100 unique IDs")
        return self


class TaskUpdate(BaseModel):
    """Payload contract for mutating an existing task with optimistic revision checks."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    status: TaskStatus | None = None
    due_date: date | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    goal_id: UUID | None = None
    entity_ids: list[UUID] | None = None
    expected_revision: int = Field(ge=1, le=9_007_199_254_740_991)

    @field_validator("due_at", "completed_at")
    @classmethod
    def instants_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive instant values while allowing explicit null to clear them."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("instant timestamps must include a timezone offset")
        return value

    @model_validator(mode="after")
    def due_fields_are_exclusive(self) -> "TaskUpdate":
        """Reject simultaneous non-null due representations in a patch."""
        if self.model_fields_set <= {"expected_revision"}:
            raise ValueError("task patch must contain at least one mutation field")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title cannot be cleared")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be cleared")
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        if self.entity_ids is not None and (
            len(set(self.entity_ids)) != len(self.entity_ids) or len(self.entity_ids) > 100
        ):
            raise ValueError("entity_ids must contain at most 100 unique IDs")
        return self


class TaskRead(BaseModel):
    """Public read projection for an owner task."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: int
    title: str
    description: str | None
    status: TaskStatus
    due_date: date | None
    due_at: datetime | None
    completed_at: datetime | None
    goal_id: UUID | None
    entity_ids: list[UUID]
    revision: int
    created_at: datetime
    updated_at: datetime


class TaskExportFence(BaseModel):
    """Bind one exported task to its version timestamps and detached content digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskExportPage(BaseModel):
    """Return one bounded task export page with immutable record fences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["tasks"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[TaskRead] = Field(max_length=100)
    fences: list[TaskExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class TaskExportValidation(BaseModel):
    """Report whether task rows and the fixed-cutoff inventory remain unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class TaskFilter(BaseModel):
    """Query parameters for filtering, sorting, and cursor-paginating tasks."""

    model_config = ConfigDict(extra="forbid")

    view: TaskView | None = None
    status: TaskStatus | None = None
    goal_id: UUID | None = None
    entity_id: UUID | None = None
    due_date_from: date | None = None
    due_date_to: date | None = None
    due_at_from: datetime | None = None
    due_at_to: datetime | None = None
    q: str | None = Field(default=None, max_length=300)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=64)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)

    @field_validator("due_at_from", "due_at_to")
    @classmethod
    def due_filters_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware instant bounds for deadline queries."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("instant filters must include a timezone offset")
        return value


class TaskPage(BaseModel):
    """Paginated collection of tasks returned to clients."""

    items: list[TaskRead]
    next_cursor: str | None = None
    total: int | None = None
