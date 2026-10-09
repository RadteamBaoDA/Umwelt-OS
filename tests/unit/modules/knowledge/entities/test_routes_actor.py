"""Review-candidate routes record the scope user, not the bootstrap owner id."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.knowledge.entities import routes

SCOPE = WorkspaceContext(user_id=7, workspace_id=uuid4(), role="owner", membership_revision=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,method", [
    (routes.assign_review_candidate, "assign_review_candidate"),
    (routes.resolve_relationship_review, "resolve_relationship_review"),
])
async def test_review_routes_pass_scope_user(route: object, method: str) -> None:
    """actor_id comes from the workspace scope even when the owner session id differs."""
    request = MagicMock()
    request.app.state.settings.multi_workspace_enabled = False
    service = MagicMock()
    target = AsyncMock(return_value=None)
    setattr(service.return_value, method, target)
    with patch.object(routes, "KnowledgeService", service):
        await route(uuid4(), MagicMock(), AsyncMock(), request, SCOPE)  # type: ignore[operator]
    assert target.call_args.kwargs["actor_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("fn", ["assign_review_candidate", "resolve_relationship_review"])
async def test_review_service_rejects_foreign_actor_before_any_read_or_write(fn: str) -> None:
    """A mismatching actor_id is a 403 straight after admission; the session is never touched."""
    from fastapi import HTTPException

    from modules.knowledge.entities import public

    session = AsyncMock()
    with patch.object(public, "_admit", AsyncMock(return_value=MagicMock())), pytest.raises(HTTPException) as err:
        await getattr(public, fn)(
            session, uuid4(), MagicMock(), scope=SCOPE, multi_workspace_enabled=False, actor_id=999,
        )
    assert err.value.status_code == 403
    for name in ("scalar", "scalars", "execute", "add", "flush", "commit"):
        getattr(session, name).assert_not_called()
