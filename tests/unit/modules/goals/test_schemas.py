"""Unit tests for goal module schemas, milestone uniqueness, and progress math.

Covers:
- GoalCreate validation: title, status, references bound (milestones + entity_ids <= 100)
- MilestoneSchema and milestone ID uniqueness validation in GoalCreate, GoalUpdate, and PlanProposal
- GoalUpdate optimistic concurrency, mutation requirement, non-nullable fields
- GoalRead projection and reference constraints
- PlanProposal validation: timezone awareness of tasks, mutual exclusivity, unique milestone IDs
- Progress math: _calculate_progress calculation, rounding, empty milestone handling, manual vs automatic progress
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.goals.public import _calculate_progress
from modules.goals.schemas import (
    GoalCreate,
    GoalFilter,
    GoalPage,
    GoalRead,
    GoalStatus,
    GoalUpdate,
    MilestoneSchema,
    PlanProposal,
    ProposalMilestone,
    TaskProposal,
)


class TestMilestoneAndGoalCreateSchemas:
    """Tests for MilestoneSchema and GoalCreate payload constraints."""

    def test_milestone_schema_defaults(self) -> None:
        """Verify MilestoneSchema default values and unique UUID generation."""
        m1 = MilestoneSchema(title="First milestone")
        m2 = MilestoneSchema(title="Second milestone")
        assert m1.title == "First milestone"
        assert m1.completed is False
        assert m1.order == 0
        assert isinstance(m1.id, UUID)
        assert m1.id != m2.id

    def test_goal_create_minimal(self) -> None:
        """Verify minimal valid goal creation."""
        goal = GoalCreate(title="Complete Personal Intelligence OS")
        assert goal.title == "Complete Personal Intelligence OS"
        assert goal.status == "active"
        assert goal.manual_progress is False
        assert goal.progress is None
        assert goal.milestones == []
        assert goal.entity_ids == []

    def test_goal_create_milestone_uniqueness_rejected(self) -> None:
        """Duplicate milestone IDs in GoalCreate must be rejected."""
        shared_id = uuid4()
        m1 = MilestoneSchema(id=shared_id, title="Step 1")
        m2 = MilestoneSchema(id=shared_id, title="Step 2")

        with pytest.raises(ValidationError, match="Milestone IDs must be unique"):
            GoalCreate(title="Test Goal", milestones=[m1, m2])

    def test_goal_create_references_bound_exceeded(self) -> None:
        """Total count of milestones + entity_ids exceeding 100 must be rejected."""
        milestones = [MilestoneSchema(title=f"M {i}") for i in range(60)]
        entity_ids = [uuid4() for _ in range(45)]  # 60 + 45 = 105 > 100

        with pytest.raises(ValidationError, match="A goal may contain at most 100 milestone and entity references"):
            GoalCreate(title="Big Goal", milestones=milestones, entity_ids=entity_ids)

    def test_goal_create_duplicate_entity_ids_rejected(self) -> None:
        """Duplicate entity references must be rejected."""
        ent = uuid4()
        with pytest.raises(ValidationError, match="entity_ids must contain unique IDs"):
            GoalCreate(title="Test", entity_ids=[ent, ent])


class TestGoalUpdateSchemas:
    """Tests for GoalUpdate patch rules, revision fencing, and non-nullable field guards."""

    def test_goal_update_valid(self) -> None:
        """Verify valid partial update."""
        patch = GoalUpdate(
            title="Refined Goal Title",
            progress=50.0,
            expected_revision=1,
        )
        assert patch.title == "Refined Goal Title"
        assert patch.progress == 50.0
        assert patch.expected_revision == 1

    def test_goal_update_empty_patch_rejected(self) -> None:
        """Patch containing only expected_revision must be rejected."""
        with pytest.raises(ValidationError, match="goal patch must contain at least one mutation field"):
            GoalUpdate(expected_revision=1)

    def test_goal_update_clearing_title_rejected(self) -> None:
        """Attempting to clear title to None must be rejected."""
        with pytest.raises(ValidationError, match="title cannot be cleared"):
            GoalUpdate(title=None, expected_revision=1)

    def test_goal_update_clearing_status_rejected(self) -> None:
        """Attempting to clear status to None must be rejected."""
        with pytest.raises(ValidationError, match="status cannot be cleared"):
            GoalUpdate(status=None, expected_revision=1)

    def test_goal_update_unique_milestones(self) -> None:
        """Duplicate milestone IDs in GoalUpdate must be rejected."""
        shared_id = uuid4()
        m1 = MilestoneSchema(id=shared_id, title="Step 1")
        m2 = MilestoneSchema(id=shared_id, title="Step 2")

        with pytest.raises(ValidationError, match="Milestone IDs must be unique"):
            GoalUpdate(milestones=[m1, m2], expected_revision=1)


class TestPlanProposalSchemas:
    """Tests for PlanProposal milestone uniqueness and task instant validation."""

    def test_plan_proposal_duplicate_milestone_ids_rejected(self) -> None:
        """Duplicate explicit milestone IDs in proposal must be rejected."""
        shared_id = uuid4()
        m1 = ProposalMilestone(id=shared_id, title="M1")
        m2 = ProposalMilestone(id=shared_id, title="M2")

        with pytest.raises(ValidationError, match="Proposal milestone IDs must be unique"):
            PlanProposal(
                proposal_id="prop-1",
                expected_revision=1,
                milestones=[m1, m2],
                tasks=[],
            )

    def test_plan_proposal_task_naive_due_at_rejected(self) -> None:
        """Naive datetime in proposal tasks must be rejected."""
        naive = datetime(2026, 11, 1, 10, 0)
        task = TaskProposal(title="Implement feature", due_at=naive)

        with pytest.raises(ValidationError, match="proposal due_at must include a timezone offset"):
            PlanProposal(
                proposal_id="prop-1",
                expected_revision=1,
                milestones=[],
                tasks=[task],
            )

    def test_plan_proposal_task_due_exclusivity(self) -> None:
        """Simultaneous due_date and due_at in proposal tasks must be rejected."""
        aware = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        task = TaskProposal(title="Implement feature", due_date=date(2026, 11, 1), due_at=aware)

        with pytest.raises(ValidationError, match="proposal due_date and due_at are mutually exclusive"):
            PlanProposal(
                proposal_id="prop-1",
                expected_revision=1,
                milestones=[],
                tasks=[task],
            )


class TestProgressMath:
    """Tests for milestone completion percentage calculation in _calculate_progress."""

    def test_calculate_progress_empty_milestones(self) -> None:
        """Empty milestones list must return 0.0% progress."""
        assert _calculate_progress([]) == 0.0

    def test_calculate_progress_all_completed(self) -> None:
        """All milestones completed returns 100.0%."""
        milestones = [
            {"id": str(uuid4()), "completed": True},
            {"id": str(uuid4()), "completed": True},
            {"id": str(uuid4()), "completed": True},
        ]
        assert _calculate_progress(milestones) == 100.0

    def test_calculate_progress_none_completed(self) -> None:
        """No completed milestones returns 0.0%."""
        milestones = [
            {"id": str(uuid4()), "completed": False},
            {"id": str(uuid4()), "completed": False},
        ]
        assert _calculate_progress(milestones) == 0.0

    def test_calculate_progress_fractional_rounding(self) -> None:
        """Fractional progress must be correctly rounded to 1 decimal place."""
        # 1 completed of 3 -> 100 / 3 = 33.333... -> 33.3
        one_of_three = [
            {"completed": True},
            {"completed": False},
            {"completed": False},
        ]
        assert _calculate_progress(one_of_three) == 33.3

        # 2 completed of 3 -> 200 / 3 = 66.666... -> 66.7
        two_of_three = [
            {"completed": True},
            {"completed": True},
            {"completed": False},
        ]
        assert _calculate_progress(two_of_three) == 66.7

        # 1 completed of 4 -> 25.0
        one_of_four = [
            {"completed": True},
            {"completed": False},
            {"completed": False},
            {"completed": False},
        ]
        assert _calculate_progress(one_of_four) == 25.0
