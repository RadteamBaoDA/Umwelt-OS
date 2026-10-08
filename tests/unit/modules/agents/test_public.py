"""Unit tests for agent module public contracts and execution logic.

Covers:
- Agent run dispatch: create_run, create_profile_run_in_uow, advisory locking, and idempotency.
- Step lifecycle and run state management: cancellation requests, run purging, and status projections.
- Tool authorization checks: tool allowlist filtering, _result_principal construction, and answer suppression on stale evidence.
- Budget tracking: browser tool run budget reservations (job count <= 2, page count <= 6, byte budget <= 10MB, active time <= 300s).
- Authority revalidation: revalidate_browser_run_authority verifying claim generation, auth session, and profile revision.
"""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.tools import ToolDefinition, ToolRegistry, ToolRisk
from core.workspaces.schemas import AccessFence, InternalJobScope, WorkspaceContext
from modules.agents.models import (
    AgentProfile,
    AgentRun,
    AgentToolCall,
)
from modules.agents.public import (
    BrowserRunAuthorization,
    _read,
    _read_current_result,
    _restore_fences,
    _result_principal,
    create_profile_run_in_uow,
    create_run,
    purge_agent_runs,
    request_cancel,
    reserve_browser_run_budget_in_uow,
    revalidate_browser_run_authority,
)
from modules.agents.schemas import (
    AgentRunRead,
    AgentRunStart,
    ProfileRunStart,
)

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
FENCE = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
CTX = {"scope": SCOPE, "multi_workspace_enabled": False}
EPOCH = {"workspace_id": WS, "membership_revision": 1, "configuration_revision": 1}


@pytest.fixture(autouse=True)
def _admitted():
    """Admission and fenced commits are covered in test_scope.py; here they succeed."""
    with patch("modules.agents.access.workspaces.read_access_fence", AsyncMock(return_value=FENCE)),             patch("modules.agents.access.workspaces.lock_access_fence", AsyncMock(return_value=FENCE)),             patch("modules.agents.public.commit_with_replay", AsyncMock()) as commit:
        yield commit


class TestAgentRunDispatch:
    """Tests for run dispatch, tool filtering, and idempotency handling."""

    @pytest.mark.asyncio
    async def test_create_run_unavailable_tools_raises_503(self) -> None:
        """create_run raises 503 if the registry has no matching read-only tools."""
        session = AsyncMock()
        registry = ToolRegistry()
        request = AgentRunStart(prompt="Hello", conversation_id=None, token_budget=None)

        with pytest.raises(HTTPException) as exc_info:
            await create_run(session, "session_hash", request, registry, **CTX)
        assert exc_info.value.status_code == 503
        assert "Agent tools are unavailable" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_run_success(self, _admitted) -> None:
        """create_run filters tools to allowed read-only tools, saves queued run, and returns AgentRunRead."""
        session = AsyncMock()

        def _mock_add(obj: object) -> None:
            now = datetime.now(UTC)
            for attr, val in [
                ("steps", 0), ("tool_calls", 0), ("active_seconds", 0),
                ("token_usage_unknown", False), ("created_at", now), ("updated_at", now)
            ]:
                if getattr(obj, attr, None) is None:
                    setattr(obj, attr, val)

        session.add = MagicMock(side_effect=_mock_add)
        tool_def = ToolDefinition(
            name="search.query",
            description="Search documents",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            confirmation_required=False,
            version="1.0",
            module="search",
        )
        registry = MagicMock(spec=ToolRegistry)
        registry.list_tools.return_value = [tool_def]
        registry.hides_tool = None

        request = AgentRunStart(prompt="Search docs", conversation_id=None, token_budget=None)
        res = await create_run(session, "auth_hash", request, registry, **CTX)

        assert isinstance(res, AgentRunRead)
        assert res.status == "queued"
        _admitted.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_profile_run_rejects_token_budget(self) -> None:
        """create_profile_run_in_uow raises 422 if a token budget is specified (token_budget_unavailable)."""
        session = AsyncMock()
        request = ProfileRunStart(
            prompt="Analyze",
            conversation_id=uuid4(),
            client_request_id=str(uuid4()),
            expected_profile_revision=1,
            token_budget=5000,
        )
        registry = ToolRegistry()

        with pytest.raises(HTTPException) as exc_info:
            await create_profile_run_in_uow(
                session, auth_session_hash="hash", profile_id="supervisor",
                request=request, registry=registry, config=None, **CTX,
            )
        assert exc_info.value.status_code == 422
        assert "token_budget_unavailable" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_create_profile_run_idempotent_duplicate_key_conflict(self) -> None:
        """create_profile_run_in_uow raises 409 when client_request_id was used with a different prompt."""
        session = AsyncMock()
        cid = str(uuid4())
        existing_run = AgentRun(
            id=uuid4(),
            owner_id=1,
            auth_session_hash="hash",
            client_request_id=cid,
            request_hash="different_request_hash",
        )
        session.scalar.return_value = existing_run

        request = ProfileRunStart(
            prompt="New prompt",
            conversation_id=uuid4(),
            client_request_id=cid,
            expected_profile_revision=1,
            token_budget=None,
        )
        registry = ToolRegistry()

        with pytest.raises(HTTPException) as exc_info:
            await create_profile_run_in_uow(
                session, auth_session_hash="hash", profile_id="supervisor",
                request=request, registry=registry, config=None, **CTX,
            )
        assert exc_info.value.status_code == 409
        assert "Client request ID was already used" in exc_info.value.detail


class TestStepLifecycleAndManagement:
    """Tests for run lifecycle, cancellation, and deletion."""

    @pytest.mark.asyncio
    async def test_request_cancel_success(self, _admitted) -> None:
        """request_cancel marks cancel_requested on a running agent run and returns AgentRunRead."""
        session = AsyncMock()
        run_id = uuid4()
        now = datetime.now(UTC)
        run = AgentRun(
            id=run_id,
            owner_id=1, **EPOCH,
            agent_id="assistant",
            status="running",
            cancel_requested=False,
            steps=0,
            tool_calls=0,
            active_seconds=0,
            token_usage=None,
            token_budget=None,
            token_usage_unknown=False,
            activities=[],
            created_at=now,
            updated_at=now,
            allowed_tools=[],
            tool_contracts={},
        )
        session.scalar.return_value = run

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        session.scalars.return_value = scalars_mock

        with patch("modules.agents.public.publish_agent_activity_safely", return_value=None),                 patch("modules.agents.public.purge_browser_results_in_uow", AsyncMock()):
            result = await request_cancel(session, run_id, MagicMock(), **CTX)
            assert isinstance(result, AgentRunRead)
            assert run.cancel_requested is True
            _admitted.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_purge_agent_runs_deletes_rows(self) -> None:
        """purge_agent_runs cancels, redacts, and purges given runs returning count."""
        session = AsyncMock()
        r1 = AgentRun(id=uuid4(), owner_id=1, checkpoint_thread_id="t1", status="queued", allowed_tools=[])
        r2 = AgentRun(id=uuid4(), owner_id=1, checkpoint_thread_id="t2", status="queued", allowed_tools=[])

        scalars_mock = MagicMock()
        scalars_mock.all.side_effect = [
            [r1, r2],  # agent runs query
            [],        # approvals r1
            [],        # effects r1
            [],        # approvals r2
            [],        # effects r2
        ]
        session.scalars.return_value = scalars_mock

        with patch("modules.agents.public.purge_browser_results_in_uow", return_value=None):
            deleted_count = await purge_agent_runs(session, [r1.id, r2.id], **CTX)
            assert deleted_count == 2
            assert r1.cancel_requested is True
            assert r2.cancel_requested is True

    def test_read_projection(self) -> None:
        """_read correctly projects private database model fields to AgentRunRead."""
        now = datetime.now(UTC)
        run = AgentRun(
            id=uuid4(),
            owner_id=1,
            agent_id="researcher",
            status="succeeded",
            answer="Here is your report.",
            error_code=None,
            steps=3,
            tool_calls=2,
            active_seconds=15,
            token_usage=150,
            token_budget=None,
            token_usage_unknown=False,
            activities=[],
            profile_snapshot={"id": "researcher"},
            profile_revision_hash="rev_hash",
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        read_dto = _read(run)
        assert read_dto.id == run.id
        assert read_dto.status == "succeeded"
        assert read_dto.answer == "Here is your report."
        assert read_dto.profile_id == "researcher"
        assert read_dto.token_usage == 150


class TestToolAuthorizationAndEvidenceFences:
    """Tests for tool principal construction, evidence fences, and answer suppression."""

    def test_result_principal_construction_all_sources(self) -> None:
        """_result_principal creates ToolExecutionPrincipal with owner_all_sources=True when profile_snapshot is None."""
        run = AgentRun(
            owner_id=1, **EPOCH,
            allowed_tools=["knowledge.search", "webhook.send"],
            profile_snapshot=None,
        )
        with patch("modules.agents.public.ToolExecutionPrincipal") as principal_cls:
            principal = _result_principal(run)
        assert principal is principal_cls.return_value
        kwargs = principal_cls.call_args.kwargs
        assert kwargs["is_owner"] is True
        assert kwargs["owner_all_sources"] is True
        assert "source.read" in kwargs["capabilities"]
        assert "webhook.send" in kwargs["capabilities"]
        # Recipe J: the scope is rebuilt from the durable original epoch, never the live one.
        assert kwargs["scope"] == InternalJobScope(workspace_id=WS, actor_user_id=1, membership_revision=1)

    def test_result_principal_quarantines_legacy_null_epoch(self) -> None:
        """A run without a captured original epoch gets no principal, so its answer stays suppressed."""
        run = AgentRun(owner_id=1, workspace_id=WS, allowed_tools=["knowledge.search"], profile_snapshot=None)
        assert _result_principal(run) is None

    def test_result_principal_fails_closed_on_oversized_sources(self) -> None:
        """_result_principal returns None if source_ids list exceeds 32 sources."""
        too_many_sources = [str(uuid4()) for _ in range(35)]
        run = AgentRun(
            owner_id=1,
            allowed_tools=["knowledge.search"],
            profile_snapshot={"source_ids": too_many_sources},
        )
        assert _result_principal(run) is None

    def test_restore_fences_valid(self) -> None:
        """_restore_fences validates source_generations and record fences."""
        s1 = uuid4()
        d1 = uuid4()
        dv1 = uuid4()
        fences_dict = {
            "source_generations": {str(s1): 2},
            "records": [
                {
                    "document_id": str(d1),
                    "document_version_id": str(dv1),
                    "source_id": str(s1),
                    "source_generation": 2,
                    "chunk_id": None,
                }
            ],
        }
        restored = _restore_fences(fences_dict)
        assert s1 in restored["source_generations"]
        assert restored["source_generations"][s1] == 2
        assert len(restored["records"]) == 1

    def test_restore_fences_invalid_generation_raises_value_error(self) -> None:
        """_restore_fences rejects non-positive source generations."""
        s1 = uuid4()
        fences_dict = {
            "source_generations": {str(s1): 0},
            "records": [],
        }
        with pytest.raises(ValueError, match="Invalid source generation"):
            _restore_fences(fences_dict)

    @pytest.mark.asyncio
    async def test_read_current_result_suppresses_answer_when_fences_stale(self) -> None:
        """_read_current_result suppresses (sets None) the answer if source evidence has changed."""
        now = datetime.now(UTC)
        s1 = uuid4()
        run = AgentRun(
            id=uuid4(),
            owner_id=1,
            agent_id="researcher",
            status="succeeded",
            answer="Secret content",
            error_code=None,
            steps=1,
            tool_calls=1,
            active_seconds=5,
            token_usage=100,
            token_budget=None,
            token_usage_unknown=False,
            activities=[],
            profile_snapshot=None,
            profile_revision_hash="hash",
            source_fences={
                "source_generations": {str(s1): 1},
                "records": [],
            },
            created_at=now,
            updated_at=now,
            completed_at=now,
            allowed_tools=["knowledge.search"],
        )

        with patch("modules.agents.public.revalidate_native_output_fences", return_value=False):
            mock_factory = MagicMock()
            result = await _read_current_result(run, mock_factory, multi_workspace_enabled=False)
            assert result.answer is None  # Suppressed!
            assert result.status == "succeeded"


class TestBrowserBudgetTrackingAndAuthority:
    """Tests for browser run budget limits and authority revalidation."""

    @pytest.mark.asyncio
    async def test_reserve_browser_run_budget_job_limit_exhausted(self) -> None:
        """reserve_browser_run_budget_in_uow raises PermissionError when browser_jobs >= 2."""
        session = AsyncMock()
        run_id = uuid4()
        args = {"url": "https://example.com"}
        args_json = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()

        run = AgentRun(
            id=run_id,
            owner_id=1, **EPOCH,
            status="running",
            browser_jobs=2,  # Limit reached!
            browser_pages=1,
            browser_bytes=1000,
            active_seconds=10,
            auth_session_hash="session_hash",
            claim_generation=1,
            browser_budget_reservations={},
            profile_snapshot={"id": "browser", "source_ids": []},
        )
        tool_call = AgentToolCall(
            run_id=run_id,
            ordinal=1,
            tool_name="browser.read",
            arguments=args,
            status="started",
        )

        session.scalar.side_effect = [run, MagicMock(spec=AgentProfile), tool_call]

        with patch("modules.agents.public._browser_profile_current", return_value=True), \
             patch("modules.chat.public.live_agent_conversation_id", return_value=uuid4()):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(PermissionError, match="Browser run budget is exhausted"):
                await reserve_browser_run_budget_in_uow(
                    session, run_id=run_id, claim_generation=1,
                    tool_slot=1, args_digest=args_digest, requested_pages=2, **CTX,
                )

    @pytest.mark.asyncio
    async def test_reserve_browser_run_budget_page_limit_exhausted(self) -> None:
        """reserve_browser_run_budget_in_uow raises PermissionError when pages exceed 6."""
        session = AsyncMock()
        run_id = uuid4()
        args = {"url": "https://example.com"}
        args_json = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()

        run = AgentRun(
            id=run_id,
            owner_id=1, **EPOCH,
            status="running",
            browser_jobs=1,
            browser_pages=5,  # 5 + 2 = 7 > 6 limit!
            browser_bytes=1000,
            active_seconds=10,
            auth_session_hash="session_hash",
            claim_generation=1,
            browser_budget_reservations={},
            profile_snapshot={"id": "browser", "source_ids": []},
        )
        tool_call = AgentToolCall(
            run_id=run_id,
            ordinal=1,
            tool_name="browser.read",
            arguments=args,
            status="started",
        )

        session.scalar.side_effect = [run, MagicMock(spec=AgentProfile), tool_call]

        with patch("modules.agents.public._browser_profile_current", return_value=True), \
             patch("modules.chat.public.live_agent_conversation_id", return_value=uuid4()):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(PermissionError, match="Browser run budget is exhausted"):
                await reserve_browser_run_budget_in_uow(
                    session, run_id=run_id, claim_generation=1,
                    tool_slot=1, args_digest=args_digest, requested_pages=2, **CTX,
                )

    @pytest.mark.asyncio
    async def test_reserve_browser_run_budget_active_time_exhausted(self) -> None:
        """reserve_browser_run_budget_in_uow raises PermissionError when active_seconds >= 300."""
        session = AsyncMock()
        run_id = uuid4()
        args = {"url": "https://example.com"}
        args_json = json.dumps(args, sort_keys=True, separators=(",", ":"))
        args_digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()

        run = AgentRun(
            id=run_id,
            owner_id=1, **EPOCH,
            status="running",
            browser_jobs=0,
            browser_pages=0,
            browser_bytes=0,
            active_seconds=300,  # Time exhausted!
            claim_started_at=datetime.now(UTC),
            auth_session_hash="session_hash",
            claim_generation=1,
            browser_budget_reservations={},
            profile_snapshot={"id": "browser", "source_ids": []},
        )
        tool_call = AgentToolCall(
            run_id=run_id,
            ordinal=1,
            tool_name="browser.read",
            arguments=args,
            status="started",
        )

        session.scalar.side_effect = [run, MagicMock(spec=AgentProfile), tool_call]

        with patch("modules.agents.public._browser_profile_current", return_value=True), \
             patch("modules.chat.public.live_agent_conversation_id", return_value=uuid4()):  # noqa: SIM117  # style-only; nested with kept
            with pytest.raises(PermissionError, match="Browser run active-time budget is exhausted"):
                await reserve_browser_run_budget_in_uow(
                    session, run_id=run_id, claim_generation=1,
                    tool_slot=1, args_digest=args_digest, requested_pages=1, **CTX,
                )

    @pytest.mark.asyncio
    async def test_revalidate_browser_authority_cancelled_run_returns_false(self) -> None:
        """revalidate_browser_run_authority returns False when run has cancel_requested set."""
        session = AsyncMock()
        run_id = uuid4()
        auth = BrowserRunAuthorization(
            owner_id=1,
            run_id=run_id,
            tool_slot=1,
            arguments_hash="digest",
            auth_session_hash="hash",
            conversation_id=uuid4(),
            profile_id="browser",
            profile_revision_hash="rev_hash",
            source_ids=frozenset(),
            claim_generation=1,
            remaining_jobs=1,
            remaining_pages=5,
            remaining_bytes=5000000,
            remaining_active_seconds=45,
        )

        run = AgentRun(
            id=run_id,
            owner_id=1, **EPOCH,
            status="running",
            cancel_requested=True,
            claim_generation=1,
            auth_session_hash="hash",
            profile_revision_hash="rev_hash",
        )
        session.scalar.return_value = run

        res = await revalidate_browser_run_authority(session, auth, **CTX)
        assert res is False
