"""Validated Pydantic DTOs for goal management and plan proposal acceptance."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.tasks.schemas import TaskRead, TaskStatus

GoalStatus = Literal["active", "completed", "paused", "cancelled"]


class MilestoneSchema(BaseModel):
    """Schema representing an ordered milestone within a goal."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=500)
    completed: bool = False
    due_date: date | None = None
    order: int = 0
    task_id: UUID | None = None


class GoalCreate(BaseModel):
    """Payload contract for creating an owner goal."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    desired_outcome: str | None = Field(default=None, max_length=10000)
    deadline: date | None = None
    progress: float | None = Field(default=None, ge=0.0, le=100.0)
    manual_progress: bool = False
    status: GoalStatus = "active"
    milestones: list[MilestoneSchema] = Field(default_factory=list, max_length=100)
    entity_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("milestones")
    @classmethod
    def unique_milestones(cls, value: list[MilestoneSchema]) -> list[MilestoneSchema]:
        """Reject duplicate milestone IDs before a goal's JSON manifest is stored."""
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("Milestone IDs must be unique")
        return value

    @model_validator(mode="after")
    def references_fit_goal_bound(self) -> "GoalCreate":
        """Keep goal-owned milestones and entity references within the public round-trip bound."""
        if len(self.milestones) + len(self.entity_ids) > 100:
            raise ValueError("A goal may contain at most 100 milestone and entity references")
        if len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError("entity_ids must contain unique IDs")
        return self


class GoalUpdate(BaseModel):
    """Payload contract for mutating an existing goal under optimistic revision control."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    desired_outcome: str | None = Field(default=None, max_length=10000)
    deadline: date | None = None
    progress: float | None = Field(default=None, ge=0.0, le=100.0)
    manual_progress: bool | None = None
    status: GoalStatus | None = None
    milestones: list[MilestoneSchema] | None = Field(default=None, max_length=100)
    entity_ids: list[UUID] | None = Field(default=None, max_length=100)
    expected_revision: int = Field(ge=1, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def patch_has_mutation(self) -> "GoalUpdate":
        """Reject revision-only no-ops and null values for non-nullable fields."""
        fields = self.model_fields_set
        if fields <= {"expected_revision"}:
            raise ValueError("goal patch must contain at least one mutation field")
        if "title" in fields and self.title is None:
            raise ValueError("title cannot be cleared")
        if "status" in fields and self.status is None:
            raise ValueError("status cannot be cleared")
        if "manual_progress" in fields and self.manual_progress is None:
            raise ValueError("manual_progress cannot be cleared")
        if self.entity_ids is not None and len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError("entity_ids must contain unique IDs")
        if self.milestones is not None and self.entity_ids is not None and (
            len(self.milestones) + len(self.entity_ids) > 100
        ):
            raise ValueError("A goal may contain at most 100 milestone and entity references")
        return self

    @field_validator("milestones")
    @classmethod
    def unique_milestones(cls, value: list[MilestoneSchema] | None) -> list[MilestoneSchema] | None:
        """Reject duplicate milestone IDs before replacing the owner's milestone list."""
        ids = [item.id for item in value or []]
        if len(set(ids)) != len(ids):
            raise ValueError("Milestone IDs must be unique")
        return value


class GoalRead(BaseModel):
    """Public read projection for an owner goal."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: int
    title: str
    description: str | None
    desired_outcome: str | None
    deadline: date | None
    progress: float
    manual_progress: bool
    status: GoalStatus
    milestones: list[MilestoneSchema] = Field(max_length=100)
    entity_ids: list[UUID] = Field(max_length=100)
    revision: int
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def references_fit_goal_bound(self) -> "GoalRead":
        """Enforce the persisted aggregate bound on public goal read projections."""
        linked_task_ids = {item.task_id for item in self.milestones if item.task_id is not None}
        if len(self.milestones) + len(linked_task_ids) + len(self.entity_ids) > 100:
            raise ValueError("A goal may contain at most 100 milestone, task, and entity references")
        return self


class TaskProposal(BaseModel):
    """Draft task item contained in a proposed plan for goal execution."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    status: TaskStatus = "todo"
    due_date: date | None = None
    due_at: datetime | None = None
    entity_ids: list[UUID] = Field(default_factory=list, max_length=100)


class ProposalMilestone(BaseModel):
    """Represent a proposed milestone optionally bound to a proposal task index."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    title: str = Field(min_length=1, max_length=500)
    due_date: date | None = None
    order: int = 0
    task_index: int | None = Field(default=None, ge=0, le=99)


class PlanProposal(BaseModel):
    """Public plan proposal schema containing milestones and actionable tasks to materialize."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1, le=9_007_199_254_740_991)
    milestones: list[ProposalMilestone] = Field(default_factory=list, max_length=100)
    tasks: list[TaskProposal] = Field(default_factory=list, max_length=100)

    @field_validator("milestones")
    @classmethod
    def unique_proposal_milestones(cls, value: list[ProposalMilestone]) -> list[ProposalMilestone]:
        """Reject duplicate explicit milestone IDs before building the acceptance manifest."""
        ids = [item.id for item in value if item.id is not None]
        if len(set(ids)) != len(ids):
            raise ValueError("Proposal milestone IDs must be unique")
        return value

    @field_validator("tasks")
    @classmethod
    def task_instants_are_aware(cls, value: list[TaskProposal]) -> list[TaskProposal]:
        """Require aware due instants and unique per-task entity references before fingerprinting."""
        for task in value:
            if task.due_at is not None and (
                task.due_at.tzinfo is None or task.due_at.utcoffset() is None
            ):
                raise ValueError("proposal due_at must include a timezone offset")
            if task.due_at is not None and task.due_date is not None:
                raise ValueError("proposal due_date and due_at are mutually exclusive")
            if len(set(task.entity_ids)) != len(task.entity_ids) or len(task.entity_ids) > 100:
                raise ValueError("proposal entity_ids must contain at most 100 unique IDs")
        return value

    @model_validator(mode="after")
    def references_fit_proposal_bound(self) -> "PlanProposal":
        """Bound proposal milestones/tasks and its distinct entity union independently."""
        if len(self.milestones) + len(self.tasks) > 100:
            raise ValueError("A proposal may contain at most 100 milestone and task references")
        entity_union = {entity_id for task in self.tasks for entity_id in task.entity_ids}
        if len(entity_union) > 100:
            raise ValueError("A proposal may reference at most 100 unique entities")
        return self


class PlanAcceptanceResult(BaseModel):
    """Outcome of atomically accepting and materializing a plan proposal."""

    goal: GoalRead
    created_tasks: list[TaskRead] = Field(max_length=100)
    deleted_task_ids: list[UUID] = Field(default_factory=list, max_length=100)
    accepted_goal_revision: int
    accepted_milestone_ids: list[UUID] = Field(default_factory=list, max_length=100)
    already_accepted: bool


class GoalFilter(BaseModel):
    """Query parameters for filtering, sorting, and cursor-paginating goals."""

    model_config = ConfigDict(extra="forbid")

    status: GoalStatus | None = None
    q: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)


class GoalPage(BaseModel):
    """Paginated collection of goals returned to clients."""

    items: list[GoalRead]
    next_cursor: str | None = None
    total: int | None = None


class GoalExportFence(BaseModel):
    """Bind one goal projection to its stored revision and exact portable content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1, le=9_007_199_254_740_991)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class GoalExportPage(BaseModel):
    """Return a bounded owner goal page with fixed-cutoff revision fences."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: int = Field(ge=1)
    record_kind: Literal["goals"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[GoalRead] = Field(max_length=100)
    fences: list[GoalExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: None = None


class GoalExportValidation(BaseModel):
    """Report whether captured goals and the cutoff-bound inventory remain unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)
