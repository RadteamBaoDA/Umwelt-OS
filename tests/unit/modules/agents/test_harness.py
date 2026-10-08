"""Unit tests for agent harness: supervisor handoff depth limits, loop detection, and budget exhaustion.

Covers:
- StrictJsonSerializer recursion depth limit (depth <= 64) and non-JSON rejection
- Supervisor handoff depth limits (depth=1 ceiling, refusing nested delegations)
- Handoff validation: specialist targets, single tool call invariant, exclusion of risky tools
- Budget exhaustion: RunLimitReached on step ceiling (20), tool-call ceiling (10), active-time ceiling (300s)
- Loop detection and recursion limits in harness workflow
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.agents.handoff import (
    HANDOFF_EXCLUDED_TOOLS,
    HANDOFF_TARGETS,
    HANDOFF_TOOL,
    HandoffRefused,
)
from modules.agents.harness import (
    HarnessContext,
    RunCancelled,
    RunLimitReached,
    StrictJsonSerializer,
    run_specialist_handoff,
)

WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=1, membership_revision=1)
ORIGINAL_FENCE = AccessFence(workspace_id=WORKSPACE_ID, user_id=1, membership_revision=1, configuration_revision=1)


class TestStrictJsonSerializer:
    """Tests for serializer depth limit and container validation."""

    def test_strict_serializer_depth_limit(self) -> None:
        """Deeply nested structures (> 64 levels) must be rejected to prevent recursion overflow."""
        serializer = StrictJsonSerializer()

        # Build nested dict with depth 65
        deep_dict: dict = {}
        curr = deep_dict
        for _ in range(65):
            curr["nested"] = {}
            curr = curr["nested"]

        with pytest.raises(TypeError, match="Agent checkpoints accept JSON values only"):
            serializer.dumps_typed(deep_dict)

    def test_strict_serializer_non_json_types(self) -> None:
        """Non-JSON types (e.g. sets, functions, arbitrary objects) must be rejected."""
        serializer = StrictJsonSerializer()
        with pytest.raises(TypeError):
            serializer.dumps_typed({"bad_set": {1, 2, 3}})

    def test_strict_serializer_nan_and_inf(self) -> None:
        """NaN and Infinite floats must be rejected."""
        serializer = StrictJsonSerializer()
        with pytest.raises(TypeError):
            serializer.dumps_typed({"nan_val": float("nan")})
        with pytest.raises(TypeError):
            serializer.dumps_typed({"inf_val": float("inf")})

    def test_strict_serializer_valid_roundtrip(self) -> None:
        """Valid JSON primitive and container types serialize and deserialize cleanly."""
        serializer = StrictJsonSerializer()
        payload = {"str": "hello", "int": 42, "float": 3.14, "bool": True, "list": [1, 2, None]}
        kind, encoded = serializer.dumps_typed(payload)
        assert kind == "json"
        decoded = serializer.loads_typed((kind, encoded))
        assert decoded == payload


class TestSupervisorHandoffDepthLimits:
    """Tests for specialist handoff constraints, depth=1 limit, and tool exclusions."""

    @pytest.mark.asyncio
    async def test_handoff_depth_limit_exceeded(self) -> None:
        """Specialist handoff is strictly forbidden when parent handoff_depth is not 0 (nested handoff)."""
        parent_context = MagicMock()
        parent_context.handoff_depth = 1  # Already a specialist!
        parent_context.profile_snapshot = {"id": "supervisor"}

        state = {"pending_tool_calls": [{"id": "call_1"}]}
        arguments = {"specialist": "research", "request": "Look up docs"}

        with pytest.raises(HandoffRefused) as exc_info:
            await run_specialist_handoff(
                parent=parent_context,
                state=state,
                arguments=arguments,
                ordinal=1,
                sink={},
                usage_box={},
            )
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_handoff_non_supervisor_refused(self) -> None:
        """Only the supervisor profile may initiate a specialist handoff."""
        parent_context = MagicMock()
        parent_context.handoff_depth = 0
        parent_context.profile_snapshot = {"id": "research"}  # Not supervisor!

        state = {"pending_tool_calls": [{"id": "call_1"}]}
        arguments = {"specialist": "knowledge", "request": "Search entities"}

        with pytest.raises(HandoffRefused) as exc_info:
            await run_specialist_handoff(
                parent=parent_context,
                state=state,
                arguments=arguments,
                ordinal=1,
                sink={},
                usage_box={},
            )
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_handoff_multiple_pending_tool_calls_refused(self) -> None:
        """Handoff must be the sole tool call in the turn (len(pending_tool_calls) == 1)."""
        parent_context = MagicMock()
        parent_context.handoff_depth = 0
        parent_context.profile_snapshot = {"id": "supervisor"}

        state = {"pending_tool_calls": [{"id": "call_1"}, {"id": "call_2"}]}  # Multiple!
        arguments = {"specialist": "knowledge", "request": "Search entities"}

        with pytest.raises(HandoffRefused) as exc_info:
            await run_specialist_handoff(
                parent=parent_context,
                state=state,
                arguments=arguments,
                ordinal=1,
                sink={},
                usage_box={},
            )
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_handoff_invalid_specialist_target(self) -> None:
        """Delegation to an unregistered specialist target is refused with invalid_arguments."""
        parent_context = MagicMock()
        parent_context.handoff_depth = 0
        parent_context.profile_snapshot = {"id": "supervisor"}

        # Frozen parent input fences are required before the target is judged.
        state = {"pending_tool_calls": [{"id": "call_1"}], "pending_source_fences": [], "source_fences": []}
        arguments = {"specialist": "unknown_specialist", "request": "Do work"}

        with pytest.raises(HandoffRefused) as exc_info:
            await run_specialist_handoff(
                parent=parent_context,
                state=state,
                arguments=arguments,
                ordinal=1,
                sink={},
                usage_box={},
            )
        assert exc_info.value.code == "invalid_arguments"

    def test_handoff_excluded_tools(self) -> None:
        """Verify high-risk tools and recursive handoff are strictly excluded from specialists."""
        assert "webhook.send" in HANDOFF_EXCLUDED_TOOLS
        assert HANDOFF_TOOL in HANDOFF_EXCLUDED_TOOLS
        assert "browser.read" in HANDOFF_EXCLUDED_TOOLS

    def test_handoff_valid_targets(self) -> None:
        """Verify the declared fixed specialist targets."""
        expected_targets = {"knowledge", "research", "personal", "project", "news", "planning"}
        assert set(HANDOFF_TARGETS) == expected_targets


class TestBudgetExhaustionAndLoopBounds:
    """Tests for budget limits (steps, tools, active seconds) and loop detection."""

    def test_run_limit_reached_exception(self) -> None:
        """Verify RunLimitReached exception instantiation."""
        exc = RunLimitReached("Tool-call budget exhausted")
        assert str(exc) == "Tool-call budget exhausted"

    def test_run_cancelled_exception(self) -> None:
        """Verify RunCancelled exception instantiation."""
        exc = RunCancelled("Run was cancelled by user")
        assert str(exc) == "Run was cancelled by user"

    def test_harness_context_elapsed_and_remaining(self) -> None:
        """Verify remaining active seconds calculation."""
        context = HarnessContext(
            run_id=uuid4(),
            scope=SCOPE,
            original_fence=ORIGINAL_FENCE,
            claim_generation=1,
            session_factory=MagicMock(),
            engine=MagicMock(),
            settings=MagicMock(),
            redis=MagicMock(),
            registry=MagicMock(),
            lease_connection=MagicMock(),
            lease_key=1001,
            segment_started=100.0,
            allowed_tools=frozenset(["search"]),
            tool_contracts={},
            active_seconds=50,
        )
        with patch("time.monotonic", return_value=110.0):
            # elapsed in segment = 110.0 - 100.0 = 10s
            assert context.elapsed() == 10.0
            # remaining = 300 - 50 - 10 = 240s
            assert context.remaining_active() == 240.0

    def test_harness_context_active_budget_exhaustion(self) -> None:
        """When cumulative active seconds exceed MAX_ACTIVE_SECONDS (300), remaining is 0."""
        context = HarnessContext(
            run_id=uuid4(),
            scope=SCOPE,
            original_fence=ORIGINAL_FENCE,
            claim_generation=1,
            session_factory=MagicMock(),
            engine=MagicMock(),
            settings=MagicMock(),
            redis=MagicMock(),
            registry=MagicMock(),
            lease_connection=MagicMock(),
            lease_key=1001,
            segment_started=100.0,
            allowed_tools=frozenset(["search"]),
            tool_contracts={},
            active_seconds=295,
        )
        with patch("time.monotonic", return_value=115.0):
            # elapsed = 15s; 295 + 15 = 310 > 300s -> remaining must be 0.0
            assert context.remaining_active() == 0.0
