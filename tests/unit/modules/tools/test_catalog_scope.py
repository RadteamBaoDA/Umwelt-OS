"""Tool catalog applies the workspace's module disables and routes use account-level auth."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.auth.dependencies import require_account, require_account_write
from core.workspaces.schemas import WorkspaceContext
from modules.tools import mcp_management_routes, routes

SCOPE = WorkspaceContext(user_id=5, workspace_id=uuid4(), role="owner", membership_revision=1)


@pytest.mark.asyncio
async def test_visible_tools_pass_workspace_modules() -> None:
    modules = {"tools": object()}
    registry = MagicMock()
    registry.list_tools.return_value = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        tool_registry=registry, mcp_runtime=None, settings=SimpleNamespace(multi_workspace_enabled=False),
    )))
    with patch.object(routes, "read_workspace_modules", AsyncMock(return_value=modules)) as read:
        await routes._workspace_tools(request, object(), SCOPE)  # type: ignore[arg-type]
    read.assert_awaited_once()
    assert read.await_args.kwargs == {"scope": SCOPE, "multi_workspace_enabled": False}
    registry.list_tools.assert_called_once_with(modules=modules)


@pytest.mark.parametrize("module", [routes, mcp_management_routes])
def test_routes_use_account_auth(module: object) -> None:
    assert module.OwnerRead.__metadata__[0].dependency is require_account  # type: ignore[attr-defined]
    assert module.OwnerWrite.__metadata__[0].dependency is require_account_write  # type: ignore[attr-defined]
