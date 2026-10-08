"""Workspace scope contract for tool principals and per-workspace module admission."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.tools.registry import ToolRegistry
from core.tools.schemas import ToolDefinition, ToolExecutionPrincipal, ToolRisk
from core.workspaces.schemas import InternalJobScope, WorkspaceContext

WS_A, WS_B = uuid4(), uuid4()
SCOPE_A = WorkspaceContext(user_id=1, workspace_id=WS_A, role="owner", membership_revision=1)
SCOPE_B = WorkspaceContext(user_id=2, workspace_id=WS_B, role="owner", membership_revision=1)


def _principal(scope: WorkspaceContext, name: str) -> ToolExecutionPrincipal:
    return ToolExecutionPrincipal(
        actor_id=f"owner:{scope.user_id}", scope=scope, is_owner=True,
        allowed_tools=frozenset({name}), source_ids=frozenset(),
        owner_all_sources=True, destinations=frozenset({"dest"}), capabilities=frozenset(),
    )


def test_principal_requires_typed_scope() -> None:
    """Scope has no default; dict/str input is rejected; both typed scopes are accepted."""
    with pytest.raises(ValidationError):
        ToolExecutionPrincipal(actor_id="owner:1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToolExecutionPrincipal(actor_id="owner:1", scope={"workspace_id": str(WS_A), "user_id": 1})  # type: ignore[arg-type]
    assert _principal(SCOPE_A, "t").scope == SCOPE_A
    job = InternalJobScope(workspace_id=WS_A, actor_user_id=1, membership_revision=1)
    assert ToolExecutionPrincipal(actor_id="owner:1", scope=job).scope == job


def _install_workspace_modules(monkeypatch: pytest.MonkeyPatch, disabled_by_call: Any) -> None:
    """Make read_module_availability answer per workspace; disabled_by_call(ws_id, n) -> bool."""
    calls: dict[Any, int] = {}

    async def read(session: object, *, scope: Any, multi_workspace_enabled: bool) -> Any:
        n = calls[scope.workspace_id] = calls.get(scope.workspace_id, 0) + 1
        disabled = disabled_by_call(scope.workspace_id, n)
        return SimpleNamespace(modules=[SimpleNamespace(id="sample", explicitly_disabled=disabled)])

    def effective(disabled: set[str], _registered: object) -> dict[str, Any]:
        return {
            mid: SimpleNamespace(id=mid, dependencies=(), enabled=mid not in disabled)
            for mid in ("tools", "sample")
        }

    monkeypatch.setattr("modules.settings.public.read_module_availability", read)
    monkeypatch.setattr("core.modules.effective_modules", effective)
    monkeypatch.setattr("core.modules.register_modules", dict)


@asynccontextmanager
async def _factory() -> Any:
    yield object()


def _context(**extra: Any) -> dict[str, Any]:
    return {
        "session_factory": _factory, "settings": SimpleNamespace(multi_workspace_enabled=True),
        "destination_id": "dest", **extra,
    }


@pytest.mark.asyncio
async def test_concurrent_workspaces_do_not_share_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A's module is revoked while it waits on approval; B's concurrent enablement cannot revive it."""
    _install_workspace_modules(monkeypatch, lambda ws, n: ws == WS_A and n >= 2)
    registry = ToolRegistry()
    ran: list[str] = []

    async def handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        ran.append(ctx["principal"].actor_id)
        return {}

    registry.register_tool(
        ToolDefinition(name="t.write", risk=ToolRisk.INTERNAL_WRITE, module="sample"), handler,
    )
    a_waiting, release = asyncio.Event(), asyncio.Event()

    async def verifier(definition: Any, args: Any, principal: Any, phase: str) -> bool:
        if principal.scope.workspace_id == WS_A and phase == "dispatch":
            a_waiting.set()
            await release.wait()
        return True

    async def current(_principal: object) -> bool:
        return True

    ctx = _context(approval_verifier=verifier, principal_revalidator=current)
    task_a = asyncio.create_task(registry.invoke_tool("t.write", {}, _principal(SCOPE_A, "t.write"), context=ctx))
    await a_waiting.wait()
    result_b = await registry.invoke_tool("t.write", {}, _principal(SCOPE_B, "t.write"), context=ctx)
    release.set()
    result_a = await task_a
    assert result_b.success is True
    assert result_a.success is False and result_a.error_code == "forbidden"
    assert ran == ["owner:2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [None, SimpleNamespace(), SimpleNamespace(multi_workspace_enabled=1)])
async def test_missing_or_non_bool_flag_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, settings: object,
) -> None:
    """The flag is never defaulted: absent or non-bool settings fail closed."""
    _install_workspace_modules(monkeypatch, lambda ws, n: False)
    registry = ToolRegistry()

    async def handler(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return {}

    registry.register_tool(ToolDefinition(name="t.read", module="sample"), handler)
    context = _context()
    context["settings"] = settings
    result = await registry.invoke_tool("t.read", {}, _principal(SCOPE_A, "t.read"), context=context)
    assert result.success is False and result.error_code == "tool_unavailable"
