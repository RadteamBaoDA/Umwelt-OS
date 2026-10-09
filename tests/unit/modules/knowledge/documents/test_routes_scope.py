"""Documents routes pass the workspace scope/flag to converted owners (no DB, no HTTP)."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from modules.knowledge.documents import routes
from tests.unit.modules.knowledge.documents._scope import SCOPE

REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
    settings=SimpleNamespace(multi_workspace_enabled=False))))
OWNER = SimpleNamespace(owner_id=1)


@pytest.mark.asyncio
async def test_dashboard_projections_route_passes_scope_not_owner_id() -> None:
    owner_call = AsyncMock(return_value="ok")
    with patch.object(routes.public, "list_gadget_document_projections", owner_call):
        await routes.list_dashboard_projections(
            session=object(), request=REQUEST, workspace=SCOPE, source_ids=[uuid4()],
        )
    assert owner_call.await_args.kwargs["scope"] == SCOPE
    assert owner_call.await_args.kwargs["multi_workspace_enabled"] is False
    assert "owner_id" not in owner_call.await_args.kwargs


@pytest.mark.asyncio
async def test_interaction_put_locks_request_then_passes_scope() -> None:
    order: list[str] = []
    lock = AsyncMock(side_effect=lambda *a, **k: order.append("lock"))
    owner_call = AsyncMock(side_effect=lambda *a, **k: order.append("call") or "ok")
    with patch.object(routes, "_lock_document_write_request", lock), \
            patch.object(routes.public, "set_gadget_document_interaction", owner_call):
        await routes.set_dashboard_document_interaction(
            uuid4(), 1, SimpleNamespace(), session=object(), request=REQUEST, workspace=SCOPE,
        )
    assert order == ["lock", "call"]
    assert owner_call.await_args.kwargs["scope"] == SCOPE
    assert owner_call.await_args.kwargs["multi_workspace_enabled"] is False
    assert "owner_id" not in owner_call.await_args.kwargs


@pytest.mark.asyncio
async def test_citation_target_route_passes_scope_and_member_reaches_grant_scoped_reader() -> None:
    reader = AsyncMock(return_value=[])
    with patch.object(routes.public, "read_chat_evidence_chunks", reader), pytest.raises(HTTPException) as exc:
        await routes.get_citation_target(
            uuid4(), session=object(), request=REQUEST, workspace=SCOPE,
            document_version_id=uuid4(), chunk_id=uuid4(),
        )
    assert exc.value.status_code == 404  # reader ran, found nothing
    assert reader.await_args.kwargs["scope"] == SCOPE
    assert reader.await_args.kwargs["multi_workspace_enabled"] is False
    member = dataclasses.replace(SCOPE, role="member")
    reader.reset_mock()
    with patch.object(routes.public, "read_chat_evidence_chunks", reader), pytest.raises(HTTPException) as denied:
        await routes.get_citation_target(
            uuid4(), session=object(), request=REQUEST, workspace=member,
            document_version_id=uuid4(), chunk_id=uuid4(),
        )
    assert denied.value.status_code == 404  # members reach the grant-scoped reader (route flip)
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_snapshot_routes_pass_scope() -> None:
    read, listing = AsyncMock(return_value=[]), AsyncMock(return_value="page")
    with patch.object(routes.public, "read_provider_snapshots", read), \
            patch.object(routes.public, "list_provider_snapshots", listing):
        await routes.read_provider_snapshots(
            SimpleNamespace(version_ids=[uuid4()]), session=object(), request=REQUEST, workspace=SCOPE,
        )
        await routes.list_provider_snapshots(
            session=object(), request=REQUEST, workspace=SCOPE, source_ids=[uuid4()],
            channel_ids=None, limit=5, cursor=None,
        )
    for call in (read, listing):
        assert call.await_args.kwargs["scope"] == SCOPE
        assert call.await_args.kwargs["multi_workspace_enabled"] is False
