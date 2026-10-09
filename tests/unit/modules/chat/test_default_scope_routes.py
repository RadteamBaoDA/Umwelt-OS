"""Private Chat/Memory/Agents routes flip to account auth + default-workspace dependencies (W4-private)."""

import inspect
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.auth.dependencies import (
    require_account,
    require_account_write,
    require_owner,
    require_owner_write,
)
from core.workspaces.dependencies import (
    require_default_workspace_read,
    require_default_workspace_write,
)
from core.workspaces.schemas import WorkspaceContext
from modules.agents import routes as agents_routes
from modules.chat import retrieval
from modules.chat import routes as chat_routes
from modules.memory import routes as memory_routes

SCOPE = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="owner", membership_revision=1)


def _dependency_calls(route) -> set:
    found: set = set()
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


@pytest.mark.parametrize("module", [chat_routes, memory_routes, agents_routes])
def test_no_route_requires_bootstrap_owner(module) -> None:
    for route in module.router.routes:
        calls = _dependency_calls(route)
        assert require_owner not in calls and require_owner_write not in calls, route.path
        assert calls & {require_account, require_account_write}, route.path


@pytest.mark.parametrize("module", [chat_routes, memory_routes, agents_routes])
def test_every_route_binds_default_workspace(module) -> None:
    default = {require_default_workspace_read, require_default_workspace_write}
    for route in module.router.routes:
        assert _dependency_calls(route) & default, route.path


async def test_account_read_dependency_returns_auth_row() -> None:
    auth = MagicMock()
    assert await chat_routes._account_read(auth, SCOPE) is auth
    assert await chat_routes._account_write(auth, SCOPE) is auth


async def test_explicit_invited_header_is_409_default_workspace_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.workspaces import dependencies as deps

    account = MagicMock(id=2, default_workspace_id=uuid4())
    monkeypatch.setattr(deps, "_request_account", AsyncMock(return_value=account))
    with pytest.raises(HTTPException) as caught:
        await deps._default_workspace(MagicMock(), MagicMock(), str(uuid4()))
    assert caught.value.status_code == 409 and caught.value.detail == "default_workspace_required"


def test_retrieval_requires_the_callers_scope() -> None:
    """R6: no owner-constant fallback; retrieval entrypoints take the actor's default scope."""
    assert not hasattr(retrieval, "owner_scope_kwargs")
    for fn in (retrieval.build_context, retrieval.revalidate_context_fence, retrieval._apply_configured_reranking):
        param = inspect.signature(fn).parameters["scope"]
        assert param.default is inspect.Parameter.empty


async def test_scope_kwargs_passes_the_given_scope_through() -> None:
    kw = await retrieval._scope_kwargs(MagicMock(), SCOPE)
    assert kw["scope"] is SCOPE
