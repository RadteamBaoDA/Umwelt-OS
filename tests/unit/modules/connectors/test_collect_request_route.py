"""C5-api-web: collection request poll route (owner only) and the scheduler gate."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.connectors import routes, scheduler
from modules.connectors.collection_schemas import CollectionRequestRead


def _http(multi: bool = False) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=multi))))


@pytest.mark.asyncio
async def test_member_gets_403_without_lookup() -> None:
    getter = AsyncMock()
    with patch.object(routes.connectors_public, "get_collection_request", getter), pytest.raises(HTTPException) as exc:
        await routes.read_collection_request(
            uuid4(), uuid4(), MagicMock(), _http(), SimpleNamespace(role="member"))  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    getter.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_reads_request_and_absent_is_404() -> None:
    source_id, request_id = uuid4(), uuid4()
    read = CollectionRequestRead(request_id=request_id, source_id=source_id, status="queued")
    getter = AsyncMock(return_value=read)
    owner = SimpleNamespace(role="owner")
    with patch.object(routes.connectors_public, "get_collection_request", getter):
        out = await routes.read_collection_request(source_id, request_id, MagicMock(), _http(), owner)  # type: ignore[arg-type]
    assert out is read
    getter.side_effect = HTTPException(404)
    with patch.object(routes.connectors_public, "get_collection_request", getter), pytest.raises(HTTPException) as exc:
        await routes.read_collection_request(source_id, request_id, MagicMock(), _http(), owner)  # type: ignore[arg-type]
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_dispatch_does_nothing_when_scheduler_disabled() -> None:
    factory = MagicMock()
    ctx = {"settings": SimpleNamespace(collector_scheduler_enabled=False, multi_workspace_enabled=False),
           "session_factory": factory}
    with (patch.object(scheduler, "_recover_expired_slots", AsyncMock()) as rec,
          patch.object(scheduler, "_create_due_requests", AsyncMock()) as create,
          patch.object(scheduler, "_enqueue_queued", AsyncMock()) as enq):
        assert await scheduler.dispatch_due_collections(ctx) == 0
    rec.assert_not_awaited()
    create.assert_not_awaited()
    enq.assert_not_awaited()
    factory.assert_not_called()
