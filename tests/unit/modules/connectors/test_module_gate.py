"""W2-S per-workspace module gate on connector scheduler entrypoints, against the real registry (no DB)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import public as connectors
from modules.connectors import scheduler, worker

WORKSPACE_ID, SOURCE_ID = uuid4(), uuid4()


def _factory(session: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _registry(disabled: bool) -> Any:
    return patch("modules.settings.lifecycle.get_disabled_modules",
                 AsyncMock(return_value=({"connectors"} if disabled else set(), 1, True)))


@pytest.mark.parametrize("disabled", [True, False])
async def test_dispatch_due_collections_checks_connectors_module(disabled: bool) -> None:
    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(SOURCE_ID, WORKSPACE_ID)])))
    session.scalar = AsyncMock(return_value=SimpleNamespace(user_id=7, revision=3))
    session.get = AsyncMock(return_value=SimpleNamespace(source_generation=1))
    opened = AsyncMock(return_value=SimpleNamespace(status="queued"))
    ctx = {"settings": SimpleNamespace(multi_workspace_enabled=False, collector_scheduler_enabled=True), "session_factory": _factory(session)}
    with (
        patch.object(scheduler, "_recover_expired_slots", AsyncMock()),
        patch.object(scheduler, "_enqueue_queued", AsyncMock(return_value=0)),
        patch.object(scheduler, "_open_request", opened),
        _registry(disabled),
    ):
        await scheduler.dispatch_due_collections(ctx)
    assert opened.await_count == (0 if disabled else 1)


@pytest.mark.parametrize("disabled", [True, False])
async def test_process_collection_request_checks_connectors_module(disabled: bool) -> None:
    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock(), execute=AsyncMock())
    request = SimpleNamespace(status="queued", workspace_id=WORKSPACE_ID, actor_user_id=7, membership_revision=3,
                              source_id=SOURCE_ID, source_generation=1)
    session.get = AsyncMock(return_value=request)
    # P3-7: the module gate runs after workspace access, so access is checked in both cases.
    access = AsyncMock() if disabled else AsyncMock(side_effect=HTTPException(404))
    ctx = {"settings": SimpleNamespace(multi_workspace_enabled=False), "session_factory": _factory(session),
           "collection_executor": AsyncMock()}
    with patch.object(connectors, "_connector_access", access), _registry(disabled):
        assert await worker.process_collection_request(ctx, str(uuid4())) == "deferred"
    assert access.await_count == 1
    if disabled:
        session.execute.assert_not_awaited()
