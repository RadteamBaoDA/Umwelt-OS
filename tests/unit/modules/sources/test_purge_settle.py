"""Unit tests for the Source purge settle gate and the durable purge fence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.sources import public, worker

WORKSPACE_ID = uuid4()
SCOPE = InternalJobScope(WORKSPACE_ID, 7, 3, None, None)


def _progress(**overrides):
    values = {
        "pending_count": 0, "failed_count": 0, "historical_failed_count": 0, "pending_owner_codes": (),
        "all_required_complete": True, "active_copy_work": False,
    }
    return SimpleNamespace(**{**values, **overrides})


def _operation(**overrides):
    values = {
        "id": uuid4(), "source_id": uuid4(), "documents_status": "deleted", "memory_status": "succeeded",
        "memory_error_code": None, "memory_cache_pending": False, "status": "running", "error_code": None,
        "pending_child_count": None, "failed_child_count": None, "pending_owner_codes": None,
    }
    return SimpleNamespace(**{**values, **overrides})


async def _settle(operation, progress):
    with patch.object(worker.documents, "source_cleanup_progress", AsyncMock(return_value=progress)) as read:
        await worker._settle_operation(None, operation, scope=SCOPE, multi_workspace_enabled=False)
    return read


async def test_settles_only_when_documents_and_memory_are_complete() -> None:
    operation = _operation()
    read = await _settle(operation, _progress())
    assert operation.status == "succeeded" and operation.error_code is None
    assert read.await_args.kwargs == {
        "source_id": operation.source_id, "capture_recorded": True, "scope": SCOPE, "multi_workspace_enabled": False,
    }


@pytest.mark.parametrize("operation,progress", [
    ({"memory_cache_pending": True}, {}),
    ({"memory_status": "running"}, {}),
])
async def test_stays_running_until_memory_coverage_and_cache_eviction_finish(operation, progress) -> None:
    op = _operation(**operation)
    await _settle(op, _progress(**progress))
    assert op.status == "running" and op.error_code is None
    assert "memory" in op.pending_owner_codes


async def test_stays_running_while_documents_stage_is_incomplete() -> None:
    op = _operation()
    await _settle(op, _progress(all_required_complete=False, pending_count=1))
    assert op.status == "running" and op.pending_child_count == 1


async def test_historical_receipt_failure_fails_the_purge() -> None:
    op = _operation()
    await _settle(op, _progress(historical_failed_count=1, all_required_complete=False))
    assert (op.status, op.error_code) == ("failed", "document_cleanup_failed")


async def test_capture_not_recorded_is_never_success() -> None:
    op = _operation(documents_status="pending")
    read = await _settle(op, _progress(all_required_complete=False, pending_owner_codes=("documents",)))
    assert read.await_args.kwargs["capture_recorded"] is False
    assert op.status == "running"


class _Session:
    def __init__(self, value) -> None:
        self.value = value

    async def scalar(self, _statement):
        return self.value


async def test_data_purge_exists_reports_any_receipt() -> None:
    fence = AccessFence(WORKSPACE_ID, 7, 3, 5)
    with patch.object(public.workspaces, "read_access_fence", AsyncMock(return_value=fence)):
        assert await public.source_data_purge_exists(
            _Session(uuid4()), uuid4(), scope=SCOPE, multi_workspace_enabled=False) is True
        assert await public.source_data_purge_exists(
            _Session(None), uuid4(), scope=SCOPE, multi_workspace_enabled=False) is False
