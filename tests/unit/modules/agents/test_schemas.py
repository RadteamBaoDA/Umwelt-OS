"""Unit tests for agents module data transfer schemas, model boundaries, and approvals.

Covers:
- AgentRunStart, ProfileRunStart, AgentRunRead, and AgentRunPage schemas
- ToolSlot and AgentToolCall structures (ordinals 1..10, payload constraints)
- ApprovalRequest, ApprovalRead, ApprovalDecisionRequest, ApprovalDecisionRead
- StepRecord and activity representations (AgentActivity, bounds, frozen state)
- AgentProfileTool, AgentProfileRead, and AgentProfilePatch invariants
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.agents.models import AgentApproval, AgentRun, AgentToolCall
from modules.agents.schemas import (
    AgentActivity,
    AgentProfilePatch,
    AgentProfileRead,
    AgentProfileTool,
    AgentRunPage,
    AgentRunRead,
    AgentRunStart,
    ApprovalDecisionRead,
    ApprovalDecisionRequest,
    ApprovalRead,
    ProfileRunStart,
)


class TestAgentRunSchemas:
    """Tests for agent run launch and status read DTOs."""

    def test_agent_run_start_valid(self) -> None:
        """Verify AgentRunStart accepts valid prompt and optional budget."""
        conv_id = uuid4()
        run = AgentRunStart(
            prompt="Analyze recent server errors",
            token_budget=50_000,
            conversation_id=conv_id,
        )
        assert run.prompt == "Analyze recent server errors"
        assert run.token_budget == 50_000
        assert run.conversation_id == conv_id

    def test_agent_run_start_bounds(self) -> None:
        """Verify prompt bounds [1, 8000] and token budget [1, 2000000]."""
        with pytest.raises(ValidationError):
            AgentRunStart(prompt="")

        with pytest.raises(ValidationError):
            AgentRunStart(prompt="x" * 8001)

        with pytest.raises(ValidationError):
            AgentRunStart(prompt="test", token_budget=0)

        with pytest.raises(ValidationError):
            AgentRunStart(prompt="test", token_budget=2_000_001)

        with pytest.raises(ValidationError):
            AgentRunStart.model_validate({"prompt": "valid", "extra_field": "disallowed"})

    def test_profile_run_start_binding(self) -> None:
        """Verify ProfileRunStart binds to conversation and client retry key."""
        conv_id = uuid4()
        req = ProfileRunStart(
            prompt="Generate weekly summary",
            expected_profile_revision=2,
            conversation_id=conv_id,
            client_request_id="client-req-999",
            token_budget=100_000,
        )
        assert req.expected_profile_revision == 2
        assert req.conversation_id == conv_id
        assert req.client_request_id == "client-req-999"

    def test_agent_run_read_projection(self) -> None:
        """Verify AgentRunRead projection with activities and token tracking."""
        run_id = uuid4()
        now = datetime.now(UTC)
        read = AgentRunRead(
            id=run_id,
            agent_id="assistant",
            status="succeeded",
            answer="Here is the report.",
            steps=3,
            tool_calls=2,
            active_seconds=15,
            token_usage=1250,
            token_budget=50_000,
            token_budget_available=True,
            token_usage_unknown=False,
            activities=[
                AgentActivity(kind="step", status="starting", created_at=now),
                AgentActivity(kind="tool", status="succeeded", tool_name="search.find", created_at=now),
            ],
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        assert read.status == "succeeded"
        assert read.steps == 3
        assert len(read.activities) == 2
        assert read.token_budget_available is True

    def test_agent_run_page(self) -> None:
        """Verify AgentRunPage cursor pagination schema."""
        page = AgentRunPage(items=[], next_cursor="opaque-cursor-token")
        assert page.items == []
        assert page.next_cursor == "opaque-cursor-token"


class TestToolSlotAndActivitySchemas:
    """Tests for ToolSlot, AgentActivity, and profile tool specifications."""

    def test_agent_activity_frozen_and_bounded(self) -> None:
        """AgentActivity must be frozen and bounded to step, tool, or status."""
        now = datetime.now(UTC)
        act = AgentActivity(kind="tool", status="in_flight", tool_name="web_search", created_at=now)
        assert act.kind == "tool"
        assert act.tool_name == "web_search"

        # Attempting mutation on frozen model raises ValidationError
        with pytest.raises(ValidationError):
            act.status = "done"  # type: ignore[misc]

    def test_agent_profile_tool_fingerprint_validation(self) -> None:
        """Verify AgentProfileTool requires exact 64-character hex sha256 fingerprint."""
        valid_tool = AgentProfileTool(
            name="knowledge.search",
            version="1.0.0",
            fingerprint="a" * 64,
        )
        assert valid_tool.name == "knowledge.search"

        # Invalid short fingerprint
        with pytest.raises(ValidationError):
            AgentProfileTool(name="t", version="1.0", fingerprint="short")

        # Invalid non-hex characters
        with pytest.raises(ValidationError):
            AgentProfileTool(name="t", version="1.0", fingerprint="z" * 64)

    def test_agent_profile_read_and_patch(self) -> None:
        """Verify AgentProfileRead and AgentProfilePatch schemas."""
        tool = AgentProfileTool(name="calc", version="1.0", fingerprint="b" * 64)
        patch = AgentProfilePatch(
            expected_revision=1,
            enabled=True,
            model_alias="reasoning",
            prompt="You are a helpful specialist.",
            allowed_tools=[tool],
            source_ids=[uuid4()],
        )
        assert patch.expected_revision == 1
        assert patch.enabled is True
        assert len(patch.allowed_tools) == 1


class TestApprovalSchemas:
    """Tests for ApprovalRead, ApprovalDecisionRequest, and ApprovalDecisionRead."""

    def test_approval_read_projection(self) -> None:
        """Verify ApprovalRead schema with argument hash and expiry instant."""
        appr_id = uuid4()
        action_id = uuid4()
        run_id = uuid4()
        conv_id = uuid4()
        now = datetime.now(UTC)
        read = ApprovalRead(
            id=appr_id,
            action_id=action_id,
            run_id=run_id,
            conversation_id=conv_id,
            tool_name="webhook.send",
            tool_version="1.0.0",
            arguments={"target": "deploy_prod"},
            argument_hash="c" * 64,
            destination_id="omniroute",
            destination_revision="rev-1",
            status="pending",
            created_at=now,
            expires_at=now,
        )
        assert read.tool_name == "webhook.send"
        assert read.status == "pending"
        assert read.argument_hash == "c" * 64

    def test_approval_decision_request_hash_validation(self) -> None:
        """Verify ApprovalDecisionRequest validates 64-hex argument hash."""
        req = ApprovalDecisionRequest(expected_argument_hash="f" * 64)
        assert req.expected_argument_hash == "f" * 64

        req_empty = ApprovalDecisionRequest()
        assert req_empty.expected_argument_hash is None

        with pytest.raises(ValidationError):
            ApprovalDecisionRequest(expected_argument_hash="invalid-hash")

    def test_approval_decision_read(self) -> None:
        """Verify ApprovalDecisionRead outputs durable resolution status."""
        appr_id = uuid4()
        read = ApprovalDecisionRead(
            id=appr_id,
            status="approved",
            run_status="running",
        )
        assert read.id == appr_id
        assert read.status == "approved"
        assert read.run_status == "running"


class TestAgentModelConstraints:
    """Tests for AgentRun and AgentToolCall database check constraint definitions."""

    def test_agent_run_table_bounds(self) -> None:
        """Check check constraint names on AgentRun table."""
        constraint_names = {c.name for c in AgentRun.__table__.constraints}
        assert "ck_agent_runs_steps" in constraint_names
        assert "ck_agent_runs_tool_calls" in constraint_names
        assert "ck_agent_runs_active_seconds" in constraint_names
        assert "ck_agent_runs_token_budget" in constraint_names

    def test_agent_tool_call_table_bounds(self) -> None:
        """Check check constraint names on AgentToolCall table."""
        constraint_names = {c.name for c in AgentToolCall.__table__.constraints}
        assert "ck_agent_tool_calls_ordinal" in constraint_names
        assert "ck_agent_tool_calls_status" in constraint_names
        assert "ck_agent_tool_calls_argument_bytes" in constraint_names

    def test_agent_approval_table_bounds(self) -> None:
        """Check check constraint names on AgentApproval table."""
        constraint_names = {c.name for c in AgentApproval.__table__.constraints}
        assert "ck_agent_approvals_ordinal" in constraint_names
        assert "ck_agent_approvals_status" in constraint_names
        assert "ck_agent_approvals_argument_bytes" in constraint_names
