"""A Source whose generation advanced must settle its queued run event instead of redispatching forever."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from core.errors import NotFoundError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.ingestion import _event_handlers, worker
from modules.ingestion._event_handlers import IngestionStageEventHandler

WS, SRC = uuid4(), uuid4()
SCOPE = InternalJobScope(workspace_id=WS, actor_user_id=7, membership_revision=3, source_id=SRC, source_generation=1)


async def test_validate_dispatch_fails_event_when_source_fence_gone() -> None:
    """Stage present but generation-bound Source fence None => event failed, committed, not dispatched."""
    event = SimpleNamespace(status="queued", payload={"run_id": str(uuid4()), "stage_id": str(uuid4())})
    session = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = SimpleNamespace(status="pending", lease_expires_at=None)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    with patch.object(_event_handlers.sources, "get_source_fence", AsyncMock(return_value=None)):
        ok = await IngestionStageEventHandler().validate_dispatch(
            session, event, SCOPE, now=datetime.now(UTC), enabled=True)  # type: ignore[arg-type]
    assert ok is False and event.status == "failed"
    session.commit.assert_awaited_once()


async def test_lock_worker_source_returns_none_on_not_found() -> None:
    """``lock_source_set`` 404 (generation advanced) means no work, not an escaping DomainError."""
    admitted = SimpleNamespace(scope=SCOPE, access_fence=AccessFence(WS, 7, 3, 5), delivery=SimpleNamespace())
    with patch.object(worker.sources, "lock_source_set", AsyncMock(side_effect=NotFoundError("gone"))):
        out = await worker._lock_worker_source(
            MagicMock(), admitted, multi_workspace_enabled=True, expected=None)  # type: ignore[arg-type]
    assert out is None
