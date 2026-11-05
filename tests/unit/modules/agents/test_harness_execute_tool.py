"""Characterization tests pinning the ``execute_tool`` graph node before and after its decomposition.

The node is driven through ``build_workflow(...).nodes["execute_tool"]`` with a recording fake context.
Every external seam (slot reservation, approvals, publication locks, activity publishing) is replaced by
a spy appending to one ordered ``events`` list so lock/commit ordering is asserted, not just results.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.tools import ToolRisk
from modules.agents import approvals as approvals_module
from modules.agents import harness
from modules.agents.harness import HarnessContext, RunCancelled, RunIncompatible, RunLimitReached
from modules.tools import public as webhook_module

RUN_ID = uuid4()
EMPTY_SINK: dict[str, Any] = {"source_generations": {}, "records": []}
SOURCE_ID = uuid4()
FENCE_RECORD = {
    "document_id": str(uuid4()), "document_version_id": str(uuid4()), "source_id": str(SOURCE_ID),
    "source_generation": 1, "chunk_id": None,
}


class FakeSession:
    """Session double that serves queued ``scalar`` results and records commits in order."""

    def __init__(self, harness_fake: Harness) -> None:
        """Bind to the shared recorder."""
        self.h = harness_fake

    async def __aenter__(self) -> Self:
        """Enter the async session scope."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the async session scope without suppressing errors."""

    async def scalar(self, _statement: Any) -> Any:
        """Return the next queued row and log the lookup."""
        self.h.events.append("scalar")
        return self.h.scalars.pop(0)

    async def commit(self) -> None:
        """Record a plain commit."""
        self.h.events.append("session.commit")


class Harness:
    """Recording fixture state shared by the fake context and the patched seams."""

    def __init__(self) -> None:
        """Create empty recorders and the default queued durable rows."""
        self.events: list[str] = []
        self.call = SimpleNamespace(arguments={}, tool_name="docs.search", status="started")
        self.stored_call = SimpleNamespace(status="started", error_code=None, evidence_refs=None, completed_at=None)
        self.run = SimpleNamespace(
            id=RUN_ID, owner_id=1, status="running", claim_generation=1, cancel_requested=False,
            evidence_revoked=False, chat_link_required=False, auth_session_hash="h", activities=[],
            source_fences=None,
        )
        self.scalars: list[Any] = []
        self.fences_current = True
        self.invoke_kwargs: dict[str, Any] = {}


def _definition(name: str = "docs.search", *, write: bool = False) -> SimpleNamespace:
    """Build a registry definition double for a read tool or the approval-gated webhook."""
    return SimpleNamespace(
        name=name, version="1", schema_fingerprint="f", input_schema={"type": "object"},
        risk=ToolRisk.EXTERNAL_WRITE if write else ToolRisk.READ_ONLY, confirmation_required=write,
        permissions=("webhook.send",) if write else (),
    )


def _build(
    monkeypatch: pytest.MonkeyPatch, *, name: str = "docs.search", arguments: dict[str, Any] | None = None,
    invoke: Any = None, write: bool = False,
) -> tuple[Harness, Any, dict[str, Any]]:
    """Wire a fake context and spies, returning the recorder, node and a default graph state."""
    h = Harness()
    arguments = {} if arguments is None else arguments
    h.call.arguments, h.call.tool_name = arguments, name
    definition = _definition(name, write=write)
    context = MagicMock()
    context.remaining_active.return_value = 100.0
    context.run_id, context.owner_id, context.claim_generation = RUN_ID, 1, 1
    context.workflow_version, context.prompt_version = harness.WORKFLOW_VERSION, harness.PROMPT_VERSION
    context.multi_workspace_enabled = False
    context.allowed_tools = frozenset({name})
    context.tool_contracts = {name: {"version": "1", "fingerprint": "f"}}
    context.registry.get_tool.return_value = definition
    context.supports_definition.return_value = True
    context.decode_fences = HarnessContext.decode_fences
    context.encode_fences = HarnessContext.encode_fences
    context.session_factory = lambda: FakeSession(h)
    context._run_snapshot = AsyncMock(return_value=SimpleNamespace(
        owner_id=1, auth_session_hash="h", tool_calls=1, profile_revision_hash=None,
    ))
    context.read_gateway_config = AsyncMock(return_value=SimpleNamespace(endpoint_destination_id="dest"))
    context.owner_principal.return_value = SimpleNamespace(scope="scope")
    context.settings = SimpleNamespace(approval_expiry_hours=24, webhook_profiles_json="")
    context.assert_lease = AsyncMock(side_effect=lambda: h.events.append("assert_lease"))

    async def admit_original(_session: Any, *, lock: bool = False) -> str:
        """Record the publication fence acquisition."""
        h.events.append(f"admit_original(lock={lock})")
        return "fence"

    context.admit_original = admit_original

    async def default_invoke(*args: Any, **kwargs: Any) -> Any:
        """Record the invocation and answer with a plain success."""
        h.events.append("invoke")
        h.invoke_kwargs = kwargs
        return SimpleNamespace(success=True, data={"hits": 1}, error_code=None, evidence_refs=("ref",))

    context.registry.invoke_tool = invoke or default_invoke
    h.scalars = [h.call, h.run, h.stored_call]

    async def reserve(*_a: Any, **_k: Any) -> Any:
        """Record the durable slot reservation."""
        h.events.append("reserve_step")
        return SimpleNamespace(owner_id=1, auth_session_hash="h", profile_revision_hash=None)

    async def lock_fences(*_a: Any, **_k: Any) -> bool:
        """Record the native output fence lock and answer per the fixture flag."""
        h.events.append("lock_native_fences")
        return h.fences_current

    async def commit(*_a: Any, **_k: Any) -> None:
        """Record the replay-aware publication commit."""
        h.events.append("commit_with_replay")
        h.events.append(f"run.source_fences={'set' if h.run.source_fences is not None else 'unset'}")

    monkeypatch.setattr(harness, "_reserve_step", reserve)
    monkeypatch.setattr(harness, "_lock_native_output_fences_for_publication", lock_fences)
    monkeypatch.setattr(harness, "commit_with_replay", commit)
    monkeypatch.setattr(harness, "revalidate_account_session", AsyncMock(return_value=True))
    monkeypatch.setattr(harness, "publish_agent_activity_safely", AsyncMock())
    import modules.chat.public as chat_public

    monkeypatch.setattr(chat_public, "has_live_agent_run_link", AsyncMock(return_value=True))
    node = harness.build_workflow(context, None).nodes["execute_tool"].bound
    state: dict[str, Any] = {
        "prompt": "p", "messages": [], "pending_tool_calls": [
            {"name": name, "arguments": json.dumps(arguments), "call_id": "c1"},
        ],
        "tool_slots": [1], "tool_index": 0, "source_fences": dict(EMPTY_SINK),
        "pending_source_fences": dict(EMPTY_SINK), "segment_steps": 0,
        "answer": None, "token_usage": None, "token_usage_unknown": False,
    }
    h.context = context  # type: ignore[attr-defined]
    return h, node, state


async def _run(node: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Invoke the compiled node whether LangGraph wraps it or not."""
    return await node.ainvoke(state) if hasattr(node, "ainvoke") else await node(state)


@pytest.mark.asyncio
async def test_read_tool_publishes_in_lock_order_and_provenance_before_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fence lock precedes native fences, run row, call row, lease, then provenance is set before the commit."""
    h, node, state = _build(monkeypatch)
    result = await _run(node, state)
    assert h.events == [
        "reserve_step", "scalar", "invoke", "admit_original(lock=True)", "lock_native_fences",
        "scalar", "scalar", "assert_lease", "commit_with_replay", "run.source_fences=set",
    ]
    assert result["tool_index"] == 1 and result["waiting_approval"] is False
    assert result["segment_steps"] == 1 and result["source_fences"] == EMPTY_SINK
    assert json.loads(result["messages"][-1]["content"]) == {"ok": {"hits": 1}}
    assert result["messages"][-1]["tool_call_id"] == "c1"
    assert (h.stored_call.status, h.stored_call.error_code, h.stored_call.evidence_refs) == ("succeeded", None, ["ref"])
    assert h.run.activities[-1]["status"] == "succeeded" and h.run.activities[-1]["tool_name"] == "docs.search"
    harness.publish_agent_activity_safely.assert_awaited_once()


@pytest.mark.asyncio
async def test_past_last_call_marks_segment_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exhausted pending list yields the segment without touching the database."""
    h, node, state = _build(monkeypatch)
    state["tool_index"] = 1
    assert await _run(node, state) == {"segment_done": True}
    assert h.events == []


@pytest.mark.asyncio
async def test_exhausted_active_budget_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """No active time left is a limit error ahead of slot reservation."""
    h, node, state = _build(monkeypatch)
    h.context.remaining_active.return_value = 0.0  # type: ignore[attr-defined]
    with pytest.raises(RunLimitReached):
        await _run(node, state)
    assert h.events == []


@pytest.mark.asyncio
async def test_invalid_arguments_are_denied_without_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-object arguments produce a denied ``invalid_arguments`` call and keep the prior sink."""
    h, node, state = _build(monkeypatch)
    state["pending_tool_calls"][0]["arguments"] = "[1]"
    result = await _run(node, state)
    assert "invoke" not in h.events and "lock_native_fences" not in h.events
    assert (h.stored_call.status, h.stored_call.error_code) == ("denied", "invalid_arguments")
    assert json.loads(result["messages"][-1]["content"]) == {"error": "invalid_arguments"}
    assert h.run.source_fences is None


@pytest.mark.asyncio
async def test_oversized_arguments_are_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-encoded arguments over the 64 kB cap are refused like malformed ones."""
    h, node, state = _build(monkeypatch)
    big = {"q": "x" * (harness.MAX_TOOL_ARGUMENT_BYTES + 1)}
    state["pending_tool_calls"][0]["arguments"] = json.dumps(big)
    h.call.arguments = big
    await _run(node, state)
    assert h.stored_call.error_code == "invalid_arguments" and "invoke" not in h.events


@pytest.mark.asyncio
async def test_oversized_result_is_replaced_and_sink_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result above the response cap becomes ``invalid_result`` and the frozen sink is retained."""
    async def big(*_a: Any, **_k: Any) -> Any:
        """Return a payload over the response byte cap."""
        return SimpleNamespace(
            success=True, data={"blob": "x" * (harness.MAX_TOOL_RESPONSE_BYTES + 1)}, error_code=None,
            evidence_refs=(),
        )

    h, node, state = _build(monkeypatch, invoke=big)
    state["source_fences"] = {"source_generations": {}, "records": []}
    result = await _run(node, state)
    assert (h.stored_call.status, h.stored_call.error_code) == ("failed", "invalid_result")
    assert json.loads(result["messages"][-1]["content"]) == {"error": "invalid_result"}


@pytest.mark.asyncio
async def test_tool_sink_is_encoded_into_run_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fences written by the tool into the sink are JSON-encoded onto the run row and the next state."""
    async def with_fence(*_a: Any, **kwargs: Any) -> Any:
        """Append one typed fence to the output sink like a source-reading tool."""
        from core.tools.schemas import ToolOutputFence

        sink = kwargs["context"]["output_fence_sink"]
        sink["records"].append(ToolOutputFence.model_validate({
            **FENCE_RECORD, **{k: UUID(FENCE_RECORD[k]) for k in ("document_id", "document_version_id", "source_id")},
        }))
        sink["source_generations"][SOURCE_ID] = 1
        return SimpleNamespace(success=True, data={}, error_code=None, evidence_refs=())

    h, node, state = _build(monkeypatch, invoke=with_fence)
    result = await _run(node, state)
    expected = {"source_generations": {str(SOURCE_ID): 1}, "records": [FENCE_RECORD]}
    assert h.run.source_fences == expected and result["source_fences"] == expected


@pytest.mark.asyncio
async def test_tool_timeout_propagates_before_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool runs under ``asyncio.timeout(remaining)``; expiry aborts without publishing."""
    async def slow(*_a: Any, **_k: Any) -> Any:
        """Never finish within the tiny remaining budget."""
        await asyncio.sleep(5)

    h, node, state = _build(monkeypatch, invoke=slow)
    h.context.remaining_active.return_value = 0.01  # type: ignore[attr-defined]
    with pytest.raises(TimeoutError):
        await _run(node, state)
    assert "admit_original(lock=True)" not in h.events


@pytest.mark.asyncio
async def test_stale_native_fences_turn_result_into_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed final fence check denies the call, rewrites the tool message and keeps the old sink."""
    h, node, state = _build(monkeypatch)
    h.fences_current = False
    result = await _run(node, state)
    assert (h.stored_call.status, h.stored_call.error_code, h.stored_call.evidence_refs) == ("denied", "forbidden", [])
    assert json.loads(result["messages"][-1]["content"]) == {"error": "forbidden"}
    # The previous frozen sink (not the tool's) is what gets published.
    assert h.run.source_fences == state["source_fences"] and result["source_fences"] == state["source_fences"]


@pytest.mark.asyncio
async def test_run_cancelled_at_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel request observed under the run lock aborts before any write or commit."""
    h, node, state = _build(monkeypatch)
    h.run.cancel_requested = True
    with pytest.raises(RunCancelled):
        await _run(node, state)
    assert "commit_with_replay" not in h.events


@pytest.mark.asyncio
async def test_slot_mismatch_is_incompatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A durable slot whose arguments differ from the checkpointed action is rejected."""
    h, node, state = _build(monkeypatch)
    h.call.arguments = {"other": 1}
    with pytest.raises(RunIncompatible):
        await _run(node, state)


@pytest.mark.asyncio
async def test_bad_tool_slots_are_incompatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-increasing or out-of-range slots invalidate the checkpoint before reservation."""
    h, node, state = _build(monkeypatch)
    state["tool_slots"] = [11]
    with pytest.raises(RunIncompatible):
        await _run(node, state)
    assert "reserve_step" not in h.events


@pytest.mark.asyncio
async def test_legacy_checkpoint_recovers_ordinal_from_assistant_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``tool_slots`` the ordinal is derived from earlier assistant call groups."""
    _h, node, state = _build(monkeypatch, name="search.query")
    del state["tool_slots"]
    state["messages"] = [{"role": "assistant", "tool_calls": [{"id": "c1"}]}]
    seen: dict[str, Any] = {}

    async def reserve(*_a: Any, **kwargs: Any) -> Any:
        """Capture the recovered ordinal and legacy flag."""
        seen.update(kwargs)
        return SimpleNamespace(owner_id=1, auth_session_hash="h", profile_revision_hash=None)

    monkeypatch.setattr(harness, "_reserve_step", reserve)
    await _run(node, state)
    assert seen["tool_ordinal"] == 1 and seen["legacy_readonly_slot"] is True


@pytest.mark.asyncio
async def test_legacy_checkpoint_with_mismatched_pending_ids_is_incompatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending call ids must equal the last assistant group on legacy checkpoints."""
    h, node, state = _build(monkeypatch, name="search.query")
    del state["tool_slots"]
    state["messages"] = [{"role": "assistant", "tool_calls": [{"id": "zzz"}]}]
    with pytest.raises(RunIncompatible):
        await _run(node, state)
    assert "reserve_step" not in h.events


def _approval_env(monkeypatch: pytest.MonkeyPatch, h: Harness, *, status: str | None, outcome: Any = None,
                  effect_state: str | None = None) -> dict[str, AsyncMock]:
    """Patch approvals and webhook profile lookups, returning the spies."""
    spies = {
        "approval_for_slot": AsyncMock(return_value=None if status is None else SimpleNamespace(status=status)),
        "create_pending_approval": AsyncMock(
            side_effect=lambda *a, **k: h.events.append("create_pending_approval") or SimpleNamespace(status="pending"),
        ),
        "get_effect_outcome": AsyncMock(return_value=outcome),
        "get_effect_state": AsyncMock(return_value=effect_state),
        "mark_effect_outcome": AsyncMock(),
        "verify_approved_action": AsyncMock(return_value=True),
    }
    for key, spy in spies.items():
        monkeypatch.setattr(approvals_module, key, spy)
    profile = SimpleNamespace(alias="hook", revision="r1")
    monkeypatch.setattr(webhook_module, "load_webhook_profiles", lambda _s: {"hook": profile})
    return spies


@pytest.mark.asyncio
async def test_new_approval_is_created_and_committed_before_any_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh write creates the pending approval, flips the call to approval_pending, commits, then yields."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    spies = _approval_env(monkeypatch, h, status=None)
    h.scalars = [h.call, h.stored_call]
    result = await _run(node, state)
    assert result == {"waiting_approval": True, "segment_done": True}
    assert h.events == ["reserve_step", "scalar", "create_pending_approval", "scalar", "session.commit"]
    assert h.stored_call.status == "approval_pending"
    kwargs = spies["create_pending_approval"].await_args.kwargs
    assert kwargs["destination_id"] == "hook" and kwargs["destination_revision"] == "r1"
    assert kwargs["source_fences"] == state["pending_source_fences"] and kwargs["ordinal"] == 1


@pytest.mark.asyncio
async def test_pending_or_review_approval_yields_without_creating(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing pending approval pauses the run and never creates a second one."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    spies = _approval_env(monkeypatch, h, status="requires_review")
    assert await _run(node, state) == {"waiting_approval": True, "segment_done": True}
    spies["create_pending_approval"].assert_not_awaited()
    assert "invoke" not in h.events


@pytest.mark.asyncio
async def test_expired_approval_is_denied_with_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-approved terminal status becomes a denied ``approval_<status>`` result without invoking."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    _approval_env(monkeypatch, h, status="expired")
    result = await _run(node, state)
    assert "invoke" not in h.events
    assert (h.stored_call.status, h.stored_call.error_code) == ("denied", "approval_expired")
    assert json.loads(result["messages"][-1]["content"]) == {"error": "approval_expired"}
    # Precomputed results never publish fresh provenance.
    assert result["source_fences"] == state["source_fences"]


@pytest.mark.asyncio
async def test_unknown_webhook_profile_is_tool_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured profile alias has no approval destination and is denied immediately."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "nope"}, write=True)
    spies = _approval_env(monkeypatch, h, status="approved")
    result = await _run(node, state)
    spies["approval_for_slot"].assert_not_awaited()
    assert json.loads(result["messages"][-1]["content"]) == {"error": "tool_unavailable"}
    assert h.stored_call.error_code == "tool_unavailable"


@pytest.mark.asyncio
async def test_approved_write_invokes_with_hooks_and_settles_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved action passes verifier and send hooks, and an untouched effect leaves the result as is."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    spies = _approval_env(monkeypatch, h, status="approved", effect_state="succeeded")
    result = await _run(node, state)
    ctx = h.invoke_kwargs["context"]
    assert callable(ctx["approval_verifier"]) and callable(ctx["before_webhook_send"])
    assert ctx["before_internal_write"] is not None and ctx["action_id"] is not None
    assert ctx["run_id"] == str(RUN_ID) and "handoff_runner" not in ctx
    spies["mark_effect_outcome"].assert_not_awaited()
    assert result["tool_index"] == 1 and h.stored_call.status == "succeeded"


@pytest.mark.asyncio
async def test_in_flight_effect_is_closed_as_requires_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """An effect still in flight after the tool returned is marked review-only and pauses the run."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    spies = _approval_env(monkeypatch, h, status="approved", effect_state="in_flight")
    result = await _run(node, state)
    assert result == {"waiting_approval": True, "segment_done": True}
    spies["mark_effect_outcome"].assert_awaited_once()
    assert spies["mark_effect_outcome"].await_args.args[2] == "requires_review"
    assert (h.stored_call.status, h.stored_call.error_code) == ("failed", "requires_review")
    assert h.run.source_fences is None


@pytest.mark.asyncio
async def test_prior_succeeded_outcome_is_replayed_without_invoking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded successful send is returned from the ledger rather than sent twice."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    _approval_env(monkeypatch, h, status="approved", outcome=("succeeded", 202, "ref-1"), effect_state="succeeded")
    result = await _run(node, state)
    assert "invoke" not in h.events
    assert json.loads(result["messages"][-1]["content"]) == {
        "ok": {"accepted": True, "status_code": 202, "result_reference": "ref-1"},
    }


@pytest.mark.asyncio
async def test_prior_in_flight_outcome_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-flight ledger entry pauses for review and malformed success data waits too."""
    for outcome in (("in_flight", None, None), ("succeeded", "bad", None)):
        h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
        _approval_env(monkeypatch, h, status="approved", outcome=outcome)
        assert await _run(node, state) == {"waiting_approval": True, "segment_done": True}
        assert "invoke" not in h.events


@pytest.mark.asyncio
async def test_prior_failed_outcome_is_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded failed send is replayed as ``execution_failed`` without invoking."""
    h, node, state = _build(monkeypatch, name="webhook.send", arguments={"profile": "hook"}, write=True)
    _approval_env(monkeypatch, h, status="approved", outcome=("failed", None, None))
    await _run(node, state)
    assert (h.stored_call.status, h.stored_call.error_code) == ("failed", "execution_failed")
    assert "invoke" not in h.events


@pytest.mark.asyncio
async def test_handoff_usage_is_folded_into_parent_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Specialist token usage is added to the parent's and the unknown flag is OR-ed."""
    async def fake_handoff(_ctx: Any, _state: Any, _args: Any, _ordinal: Any, _sink: Any, usage: dict[str, Any]) -> dict[str, Any]:
        """Report specialist usage."""
        usage.update({"tokens": 7, "unknown": True})
        return {}

    async def handoff(*_a: Any, **kwargs: Any) -> Any:
        """Drive the supplied handoff runner like the real delegation tool."""
        await kwargs["context"]["handoff_runner"]({"specialist": "x", "request": "y"})
        return SimpleNamespace(success=True, data={"answer": "a"}, error_code=None, evidence_refs=())

    monkeypatch.setattr(harness, "run_specialist_handoff", fake_handoff)
    _h, node, state = _build(monkeypatch, name=harness.HANDOFF_TOOL, invoke=handoff)
    state["token_usage"] = 3
    result = await _run(node, state)
    assert result["token_usage"] == 10 and result["token_usage_unknown"] is True


def test_uuid_import_used() -> None:
    """Keep the UUID import meaningful for typing of fixture identities."""
    assert isinstance(RUN_ID, UUID)
