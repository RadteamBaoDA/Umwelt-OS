"""Characterization tests for ``provisioning.drive_workflow_operation`` step driving and journaling.

The driver was split into lookup/send/failure helpers. These tests pin, through the public entry
point, which n8n calls happen for each step kind, which outcome is journaled through
``_settle_retained_workflow_after_io`` and when the loop stops (four-step cap, fences, failures).
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import provisioning

SOURCE_ID = uuid4()
WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3,
                         source_id=SOURCE_ID, source_generation=2)
FENCE = AccessFence(WORKSPACE_ID, 7, 3, 5)
ORIGINAL: dict[str, object] = {"id": "op"}


def _operation(kind: str, *, target: str | None = None, body: object = None) -> dict[str, object]:
    return {"workflow_name": "wf", "step": {"kind": kind, "target": target, "request": body}}


def _api(**over: Any) -> MagicMock:
    api = MagicMock(service_url="http://n8n")
    api.create_workflow = AsyncMock(return_value="wf-1")
    api.update_workflow = AsyncMock()
    api.set_active = AsyncMock()
    api.get_workflow = AsyncMock(return_value={"nodes": []})
    for key, value in over.items():
        setattr(api, key, value)
    return api


async def _drive(claims: list[Any], *, api: Any = None, allowed: list[bool] | None = None,
                 settle: list[Any] | None = None, matches: list[dict[str, Any]] | None = None,
                 workflow_matches: bool = True) -> tuple[bool, dict[str, Any]]:
    spies = {
        "claim": AsyncMock(side_effect=claims),
        "allowed": AsyncMock(side_effect=allowed if allowed is not None else [True] * 8),
        "settle": AsyncMock(side_effect=settle if settle is not None else [(True, None)] * 8),
        "find": AsyncMock(return_value=matches or []),
    }
    session = SimpleNamespace(rollback=AsyncMock())
    spies["session"] = session
    with patch.object(provisioning, "claim_workflow_step", spies["claim"]), \
            patch.object(provisioning, "_workflow_send_allowed", spies["allowed"]), \
            patch.object(provisioning, "_settle_retained_workflow_after_io", spies["settle"]), \
            patch("modules.connectors.n8n.find_workflows_by_name", spies["find"]), \
            patch("modules.connectors.n8n.workflow_matches", return_value=workflow_matches):
        result = await provisioning.drive_workflow_operation(
            session, SOURCE_ID, api or _api(), scope=SCOPE, multi_workspace_enabled=False,  # type: ignore[arg-type]
            original_operation=ORIGINAL, access_fence=FENCE,
        )
    return result, spies


def _outcomes(spies: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(c.kwargs["outcome"], c.kwargs) for c in spies["settle"].await_args_list]


@pytest.mark.asyncio
async def test_missing_claim_rolls_back_and_returns_false() -> None:
    result, spies = await _drive([None])
    assert result is False and spies["session"].rollback.await_count == 1
    spies["settle"].assert_not_awaited()


@pytest.mark.asyncio
async def test_first_send_fence_denial_journals_not_sent() -> None:
    result, spies = await _drive([_operation("create", body={})], allowed=[False])
    assert result is False
    assert _outcomes(spies)[0][1]["error_code"] == "original_effect_send_fenced"
    assert spies["allowed"].await_args.kwargs["transport_entered"] is False


@pytest.mark.asyncio
async def test_non_dict_step_stops_without_io() -> None:
    result, spies = await _drive([{"step": None}])
    assert result is False
    spies["settle"].assert_not_awaited()


@pytest.mark.asyncio
async def test_create_success_settles_known_success_with_new_workflow_id() -> None:
    api = _api()
    result, spies = await _drive([_operation("create", body={"nodes": []})], api=api)
    assert result is True
    api.create_workflow.assert_awaited_once_with({"nodes": []})
    assert _outcomes(spies)[0][0] == "known_success" and _outcomes(spies)[0][1]["remote_id"] == "wf-1"


@pytest.mark.asyncio
async def test_update_activate_deactivate_call_the_matching_api() -> None:
    api = _api()
    await _drive([_operation("update", target="t", body={"a": 1})], api=api)
    api.update_workflow.assert_awaited_once_with("t", {"a": 1})
    api2 = _api()
    _, spies = await _drive([_operation("activate", target="t")], api=api2)
    api2.set_active.assert_awaited_once_with("t", True)
    assert _outcomes(spies)[0][1]["remote_id"] is None
    api3 = _api()
    await _drive([_operation("deactivate", target="t")], api=api3)
    api3.set_active.assert_awaited_once_with("t", False)


@pytest.mark.asyncio
async def test_loop_chains_next_operations_and_stops_after_four_steps() -> None:
    nxt = _operation("activate", target="t")
    result, spies = await _drive([_operation("create", body={})] + [nxt] * 3, settle=[(True, nxt)] * 8)
    assert result is False
    assert spies["claim"].await_count == 4
    assert spies["claim"].await_args_list[1].kwargs["original_operation"] is nxt


@pytest.mark.asyncio
async def test_unchanged_settlement_returns_false_and_final_none_returns_true() -> None:
    result, _ = await _drive([_operation("create", body={})], settle=[(False, None)])
    assert result is False
    result2, _ = await _drive([_operation("create", body={}), _operation("activate", target="t")],
                              settle=[(True, _operation("activate", target="t")), (True, None)])
    assert result2 is True


@pytest.mark.asyncio
async def test_lookup_without_match_continues_with_create() -> None:
    body = {"nodes": []}
    result, spies = await _drive([_operation("lookup", body=body)], matches=[])
    assert result is True
    kwargs = spies["settle"].await_args.kwargs
    assert (kwargs["outcome"], kwargs["next_kind"], kwargs["remote_id"], kwargs["request"]) == ("known_success", "create", None, body)


@pytest.mark.asyncio
async def test_lookup_single_verified_match_continues_with_update() -> None:
    body = {"nodes": []}
    api = _api()
    result, spies = await _drive([_operation("lookup", body=body)], api=api, matches=[{"id": "w9"}])
    assert result is True
    api.get_workflow.assert_awaited_once_with("w9")
    kwargs = spies["settle"].await_args.kwargs
    assert (kwargs["next_kind"], kwargs["remote_id"]) == ("update", "w9")
    assert [c.kwargs["transport_entered"] for c in spies["allowed"].await_args_list] == [False, True]


@pytest.mark.asyncio
async def test_lookup_second_fence_denial_journals_not_sent_and_stops() -> None:
    result, spies = await _drive([_operation("lookup", body={})], allowed=[True, False], matches=[{"id": "w9"}])
    assert result is False
    assert [o[0] for o in _outcomes(spies)] == ["not_sent"]
    assert spies["settle"].await_args.kwargs["error_code"] == "original_effect_send_fenced"


@pytest.mark.asyncio
@pytest.mark.parametrize(("matches", "workflow_matches", "body"), [
    ([{"id": "a"}, {"id": "b"}], True, {}),   # ambiguous
    ([{"id": ""}], True, {}),                 # missing id
    ([{"id": "a"}], True, None),              # invalid prepared body
    ([{"id": "a"}], False, {}),               # mismatched identity
])
async def test_lookup_validation_failures_are_journaled_as_known_rejection(matches: list, workflow_matches: bool, body: object) -> None:
    result, spies = await _drive([_operation("lookup", body=body)], matches=matches, workflow_matches=workflow_matches)
    assert result is False
    assert _outcomes(spies)[0][0] == "known_rejection" and _outcomes(spies)[0][1]["error_code"] == "n8n_outcome_unknown"


@pytest.mark.asyncio
async def test_transport_failures_are_classified() -> None:
    def status(code: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "http://n8n")
        return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))

    for exc, outcome, code in (
        (status(400), "known_rejection", "n8n_request_rejected"),
        (status(408), "unknown", "n8n_outcome_unknown"),
        (status(500), "unknown", "n8n_outcome_unknown"),
        (httpx.ConnectError("down"), "unknown", "n8n_outcome_unknown"),
    ):
        api = _api(create_workflow=AsyncMock(side_effect=exc))
        result, spies = await _drive([_operation("create", body={})], api=api)
        assert result is False
        assert (_outcomes(spies)[0][0], _outcomes(spies)[0][1]["error_code"]) == (outcome, code)


@pytest.mark.asyncio
async def test_invalid_body_and_unsupported_kind_are_unknown_outcomes() -> None:
    result, spies = await _drive([_operation("create", body="not-a-dict")])
    assert result is False and _outcomes(spies)[0][0] == "unknown"
    result2, spies2 = await _drive([_operation("explode", target="t")])
    assert result2 is False and _outcomes(spies2)[0][0] == "unknown"
    result3, spies3 = await _drive([_operation("activate", target=None)])
    assert result3 is False and _outcomes(spies3)[0][0] == "unknown"


@pytest.mark.asyncio
async def test_cancellation_is_journaled_unknown_and_reraised() -> None:
    api = _api(create_workflow=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await _drive([_operation("create", body={})], api=api)


@pytest.mark.asyncio
async def test_retained_admission_denial_propagates_without_journaling() -> None:
    denied = provisioning.RetainedEffectAdmissionDenied(status_code=403, detail="lost")
    api = _api(create_workflow=AsyncMock(side_effect=denied))
    with pytest.raises(provisioning.RetainedEffectAdmissionDenied):
        await _drive([_operation("create", body={})], api=api)
