"""Tests for the unknown-create workflow discovery helper extracted from ``_recover_unknown_create``."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from core.model_gateway.transport import EndpointNetworkPolicyError
from modules.connectors import worker

SOURCE_ID = uuid4()
OPERATION_ID = uuid4()
ORIGINAL = {"id": str(OPERATION_ID), "workflow_name": "wf"}
STEP = {"request": {"nodes": []}}


@pytest.mark.asyncio
async def test_single_exact_match_is_returned() -> None:
    api = AsyncMock()
    api.get_workflow.return_value = {"nodes": []}
    with (
        patch.object(worker, "find_workflows_by_name", AsyncMock(return_value=[{"id": "w1"}])),
        patch.object(worker, "workflow_matches", return_value=True),
    ):
        assert await worker._discover_created_workflow(api, ORIGINAL, STEP, SOURCE_ID) == (
            OPERATION_ID,
            "w1",
        )


@pytest.mark.asyncio
async def test_ambiguous_or_mismatched_matches_leave_workflow_unset() -> None:
    api = AsyncMock()
    with patch.object(
        worker, "find_workflows_by_name", AsyncMock(return_value=[{"id": "a"}, {"id": "b"}])
    ):
        assert await worker._discover_created_workflow(api, ORIGINAL, STEP, SOURCE_ID) == (
            OPERATION_ID,
            None,
        )
    with (
        patch.object(worker, "find_workflows_by_name", AsyncMock(return_value=[{"id": "a"}])),
        patch.object(worker, "workflow_matches", return_value=False),
    ):
        assert await worker._discover_created_workflow(api, ORIGINAL, STEP, SOURCE_ID) == (
            OPERATION_ID,
            None,
        )


@pytest.mark.asyncio
async def test_transport_and_shape_errors_are_logged_at_debug_and_leave_workflow_unset() -> None:
    api = AsyncMock()
    with (
        patch.object(worker.logger, "debug") as debug,
        patch.object(
            worker, "find_workflows_by_name", AsyncMock(side_effect=httpx.ConnectError("down"))
        ),
    ):
        assert await worker._discover_created_workflow(api, ORIGINAL, STEP, SOURCE_ID) == (
            OPERATION_ID,
            None,
        )
    debug.assert_called_once()
    assert debug.call_args.args[0].startswith("connector_unknown_create_lookup_failed")
    assert debug.call_args.kwargs == {"exc_info": True}
    # An unparseable operation id fails before the lookup and yields no operation id either.
    assert await worker._discover_created_workflow(api, {"id": "not-a-uuid"}, STEP, SOURCE_ID) == (
        None,
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [EndpointNetworkPolicyError("address is denied"), RuntimeError("boom")]
)
async def test_egress_policy_and_runtime_errors_keep_create_unknown(error: Exception) -> None:
    """Egress-policy failures must not escape and abort the reconcile pass (old ``except Exception``)."""
    api = AsyncMock()
    with (
        patch.object(worker.logger, "debug") as debug,
        patch.object(worker, "find_workflows_by_name", AsyncMock(side_effect=error)),
    ):
        assert await worker._discover_created_workflow(api, ORIGINAL, STEP, SOURCE_ID) == (
            OPERATION_ID,
            None,
        )
    debug.assert_called_once()
    assert debug.call_args.kwargs == {"exc_info": True}
