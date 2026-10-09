"""Workspace-scope contracts for the MCP repository, dispatch and runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from core.tools.schemas import ToolExecutionPrincipal
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.tools import mcp_repository, mcp_runtime
from modules.tools.mcp_dispatch import McpDispatchAdapter
from modules.tools.mcp_runtime import McpRuntime
from modules.tools.mcp_schemas import (
    ConnectionDraft,
    DiscoveryPersist,
    GrantChoice,
    InboundBinding,
    InboundClientCreate,
    InboundPrincipal,
    McpRisk,
)
from modules.tools.mcp_transport import McpTransportError

WS_A, WS_B = uuid4(), uuid4()
OWNER_A = WorkspaceContext(user_id=7, workspace_id=WS_A, role="owner", membership_revision=1)
OWNER_B = WorkspaceContext(user_id=7, workspace_id=WS_B, role="owner", membership_revision=1)
MEMBER_A = WorkspaceContext(user_id=8, workspace_id=WS_A, role="member", membership_revision=1)


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _session() -> MagicMock:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.scalars = AsyncMock(return_value=MagicMock(all=list))
    session.flush = AsyncMock()
    return session


# 1. repository ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_connection_select_carries_workspace_and_owner() -> None:
    session = _session()
    with pytest.raises(mcp_repository.McpNotFound):
        await mcp_repository.get_connection(session, uuid4(), scope=OWNER_A)
    sql = _sql(session.scalar.await_args.args[0])
    assert "mcp_connections.workspace_id = " in sql
    assert "mcp_connections.owner_id = " in sql


@pytest.mark.asyncio
async def test_member_denied_before_any_query() -> None:
    session = _session()
    with pytest.raises(HTTPException) as exc:
        await mcp_repository.list_connections(session, scope=MEMBER_A)
    assert exc.value.status_code == 403
    session.scalar.assert_not_awaited()
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_client_inserts_carry_workspace_and_actor() -> None:
    session = _session()
    data = InboundClientCreate.model_construct(
        name="c", audience="https://x/api/v1/mcp/", tool_bindings=(), source_ids=(), capabilities=(),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    with patch.object(mcp_repository, "InboundClientIssued"), patch.object(mcp_repository, "to_inbound_read"):
        await mcp_repository.create_inbound_client(session, data, scope=InternalJobScope(WS_B, 9, 1))
    row = session.add.call_args.args[0]
    assert (row.workspace_id, row.owner_id) == (WS_B, 9)


@pytest.mark.asyncio
async def test_save_connection_insert_and_credential_aad_stay_owner_based() -> None:
    session = _session()
    session.scalar = AsyncMock(return_value=True)  # advisory lock acquired
    draft = ConnectionDraft.model_validate({
        "name": "n", "transport": "streamable_http", "endpoint": "https://example.com/mcp",
        "auth_method": "bearer",
        "credential_update": {"action": "replace", "value": SecretStr("tok")},
    })
    with patch.object(mcp_repository, "encrypt_connection_credential", return_value="ct") as enc, \
            patch.object(mcp_repository, "to_connection_read", return_value=MagicMock()):
        await mcp_repository.save_connection(session, None, 0, draft, scope=OWNER_A, encryption_key="k")
    row = session.add.call_args.args[0]
    assert (row.workspace_id, row.owner_id) == (WS_A, 7)
    assert enc.call_args.kwargs["owner_id"] == 7  # AAD is the integer actor, not the workspace
    assert "mcp_connections.workspace_id = " in _sql(session.scalars.await_args.args[0])


# 2. dispatch -----------------------------------------------------------------------------------

def _principal(scope: WorkspaceContext) -> ToolExecutionPrincipal:
    return ToolExecutionPrincipal(
        actor_id="owner:7", scope=scope, is_owner=True, owner_all_sources=True,
        allowed_tools=frozenset(), destinations=frozenset({"local"}),
    )


def _adapter() -> tuple[McpDispatchAdapter, AsyncMock, MagicMock]:
    resolve = AsyncMock(side_effect=mcp_repository.McpConflict("stop"))
    client = MagicMock()
    client.call_tool = AsyncMock()
    adapter = McpDispatchAdapter(MagicMock(), client, resolve_fence=resolve, revalidate_fence=AsyncMock())
    return adapter, resolve, client


def _context(scope: WorkspaceContext) -> dict[str, Any]:
    return {"principal": _principal(scope), "destination_id": "local", "principal_revalidator": AsyncMock()}


@pytest.mark.asyncio
async def test_invoke_tool_rejects_other_workspace_before_any_fence_or_client_call() -> None:
    adapter, resolve, client = _adapter()
    with pytest.raises(McpTransportError, match="outside the caller workspace"):
        await adapter._invoke_tool(WS_A, uuid4(), uuid4(), uuid4(), "tool", "k", "h", {}, _context(OWNER_B))
    resolve.assert_not_awaited()
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_tool_same_workspace_resolves_fence_with_principal_scope() -> None:
    adapter, resolve, _ = _adapter()
    with pytest.raises(mcp_repository.McpConflict):
        await adapter._invoke_tool(WS_A, uuid4(), uuid4(), uuid4(), "tool", "k", "h", {}, _context(OWNER_A))
    assert resolve.await_args.args[0] == OWNER_A


# 3. runtime ------------------------------------------------------------------------------------

def _runtime(flag: bool = False) -> McpRuntime:
    runtime = object.__new__(McpRuntime)
    runtime.settings = SimpleNamespace(multi_workspace_enabled=flag)  # type: ignore[assignment]

    @asynccontextmanager
    async def factory() -> Any:
        yield MagicMock()

    runtime.session_factory = factory  # type: ignore[assignment]
    runtime.registry = MagicMock()
    runtime.registry.list_tools.return_value = []
    return runtime


def _identity(owner_id: int = 7) -> InboundPrincipal:
    return InboundPrincipal(
        client_id=uuid4(), workspace_id=WS_A, owner_id=owner_id, audience="https://x/api/v1/mcp/",
        revision=1, bindings=(), source_ids=(), capabilities=(), destination_id="mcp-client:x",
    )


@pytest.mark.asyncio
async def test_authorize_inbound_builds_scope_from_client_row_workspace_and_owner() -> None:
    runtime = _runtime(flag=True)
    owner = SimpleNamespace(user_id=7, membership_revision=4)
    page = SimpleNamespace(items=[], next_cursor=None)
    with patch.object(mcp_runtime.mcp_repository, "revalidate_inbound_principal", AsyncMock(return_value=True)), \
            patch.object(mcp_runtime.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)) as resolve, \
            patch.object(mcp_runtime.sources_public, "list_tool_sources", AsyncMock(return_value=page)) as listing:
        principal = await runtime.authorize_inbound(_identity())
    assert principal is not None
    assert principal.scope == InternalJobScope(WS_A, 7, 4)
    assert resolve.await_args.args[1] == WS_A
    assert listing.await_args.kwargs["scope"] == principal.scope
    assert listing.await_args.kwargs["multi_workspace_enabled"] is True


@pytest.mark.asyncio
async def test_authorize_inbound_owner_mismatch_returns_none() -> None:
    runtime = _runtime()
    owner = SimpleNamespace(user_id=99, membership_revision=4)
    with patch.object(mcp_runtime.mcp_repository, "revalidate_inbound_principal", AsyncMock(return_value=True)), \
            patch.object(mcp_runtime.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=owner)), \
            patch.object(mcp_runtime.sources_public, "list_tool_sources", AsyncMock()) as listing:
        assert await runtime.authorize_inbound(_identity()) is None
    listing.assert_not_awaited()


def _output_case() -> tuple[McpRuntime, InboundPrincipal, ToolExecutionPrincipal, InboundBinding]:
    runtime = _runtime(flag=True)
    binding = InboundBinding(name="search.query", version="1", schema_fingerprint="a" * 64)
    identity = _identity().model_copy(update={"bindings": (binding,), "capabilities": ("source.read",)})
    expected = ToolExecutionPrincipal(
        actor_id="mcp-client:x", scope=InternalJobScope(WS_A, 7, 1), allowed_tools=frozenset({"search.query"}),
    )
    definition = SimpleNamespace(
        schema_fingerprint="a" * 64, risk=mcp_runtime.ToolRisk.READ_ONLY, confirmation_required=False,
        permissions=(),
    )
    runtime.registry.get_tool.return_value = definition
    runtime.revalidate_inbound = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return runtime, identity, expected, binding


@pytest.mark.asyncio
async def test_output_fence_revalidation_receives_flag_and_type_errors_propagate() -> None:
    runtime, identity, expected, binding = _output_case()
    with patch("modules.tools.public.revalidate_native_output_fences", AsyncMock(return_value=True)) as fences:
        ok = await runtime.revalidate_inbound_output(identity, expected, binding=binding, sink={"x": 1})
    assert ok is True
    assert fences.await_args.kwargs["multi_workspace_enabled"] is True
    with patch("modules.tools.public.revalidate_native_output_fences", AsyncMock(side_effect=TypeError("bug"))), \
            pytest.raises(TypeError):
        await runtime.revalidate_inbound_output(identity, expected, binding=binding, sink={"x": 1})
    with patch("modules.tools.public.revalidate_native_output_fences", AsyncMock(side_effect=HTTPException(403))):
        assert await runtime.revalidate_inbound_output(identity, expected, binding=binding, sink={"x": 1}) is False


@pytest.mark.asyncio
async def test_hydrate_pages_100_workspaces_and_skips_owner_mismatch() -> None:
    runtime = _runtime()
    statements: list[Any] = []
    result = MagicMock()
    result.all.return_value = [(WS_A, 7), (WS_B, 7)]

    @asynccontextmanager
    async def factory() -> Any:
        session = MagicMock()

        async def execute(stmt: Any) -> Any:
            statements.append(stmt)
            return result

        session.execute = execute
        yield session

    runtime.session_factory = factory  # type: ignore[assignment]
    owners = {WS_A: SimpleNamespace(user_id=7, membership_revision=2), WS_B: SimpleNamespace(user_id=99, membership_revision=2)}
    resolve = AsyncMock(side_effect=lambda _s, ws, **_k: owners[ws])
    runtime.refresh_connection = AsyncMock()  # type: ignore[method-assign]
    runtime._admit = AsyncMock()  # type: ignore[method-assign]
    with patch.object(mcp_runtime.workspaces, "resolve_workspace_owner_context", resolve), \
            patch.object(mcp_runtime.mcp_repository, "list_runtime_connections",
                         AsyncMock(return_value=(SimpleNamespace(id=uuid4()),))):
        await runtime.hydrate_connections()
    sql = _sql(statements[0])
    assert "DISTINCT mcp_connections.workspace_id, mcp_connections.owner_id" in sql
    assert statements[0]._limit_clause.value == 100
    runtime.refresh_connection.assert_awaited_once()  # type: ignore[attr-defined]
    assert runtime.refresh_connection.await_args.args[0] == InternalJobScope(WS_A, 7, 2)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hydrate_continues_after_one_workspace_fails() -> None:
    runtime = _runtime()
    result = MagicMock()
    result.all.return_value = [(WS_A, 7), (WS_B, 7)]

    @asynccontextmanager
    async def factory() -> Any:
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        yield session

    runtime.session_factory = factory  # type: ignore[assignment]
    resolve = AsyncMock(side_effect=lambda _s, ws, **_k: SimpleNamespace(user_id=7, membership_revision=2))
    runtime.refresh_connection = AsyncMock()  # type: ignore[method-assign]
    runtime._admit = AsyncMock(side_effect=[mcp_repository.McpConflict("busy"), None])  # type: ignore[method-assign]
    with patch.object(mcp_runtime.workspaces, "resolve_workspace_owner_context", resolve), \
            patch.object(mcp_runtime.mcp_repository, "list_runtime_connections",
                         AsyncMock(return_value=(SimpleNamespace(id=uuid4()),))):
        await runtime.hydrate_connections()
    runtime.refresh_connection.assert_awaited_once()  # type: ignore[attr-defined]
    assert runtime.refresh_connection.await_args.args[0].workspace_id == WS_B  # type: ignore[attr-defined]


# 3. catalog visibility (P2-2) ------------------------------------------------------------------

def _registered_adapter() -> tuple[McpDispatchAdapter, str]:
    adapter = McpDispatchAdapter(MagicMock(), MagicMock(), resolve_fence=AsyncMock(), revalidate_fence=AsyncMock())
    conn_id = uuid4()
    connection = SimpleNamespace(id=conn_id, enabled=True, revision=1)
    capability = SimpleNamespace(id=uuid4(), kind="tool", remote_key="t", descriptor_hash="h")
    discovery = SimpleNamespace(id=uuid4(), connection_id=conn_id, connection_revision=1, capabilities=(capability,))
    grant = SimpleNamespace(capability_id=capability.id, connection_id=conn_id, descriptor_hash="h",
                            reviewed_connection_revision=1, revoked_at=None, expires_at=None,
                            risk=McpRisk.READ_ONLY, purpose="chat", id=uuid4())
    name = f"mcp.{conn_id.hex}.{capability.id.hex}"
    with patch.object(adapter, "_build_definition", return_value=SimpleNamespace(name=name)), \
            patch.object(adapter, "_build_async_handler"):
        adapter.register_selected_capabilities(WS_A, connection, discovery, (grant,))  # type: ignore[arg-type]
    return adapter, name


def test_dispatch_hides_mcp_tools_of_other_workspaces_only() -> None:
    adapter, name = _registered_adapter()
    assert adapter.hides(name, WS_B) is True
    assert adapter.hides(name, WS_A) is False
    assert adapter.hides("source.read", WS_B) is False
    assert adapter.hides(f"mcp.{uuid4().hex}.{uuid4().hex}", WS_A) is True  # unknown connection


@pytest.mark.asyncio
async def test_tools_listing_and_invoke_allowlist_hide_other_workspace_mcp_tools() -> None:
    from modules.tools import routes
    adapter, name = _registered_adapter()
    native = SimpleNamespace(name="source.read", model_dump=lambda mode="json": {"name": "source.read"})
    mcp = SimpleNamespace(name=name, model_dump=lambda mode="json": {"name": name})
    registry = MagicMock()
    registry.list_tools.return_value = [native, mcp]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        tool_registry=registry, mcp_runtime=SimpleNamespace(dispatch=adapter),
        settings=SimpleNamespace(multi_workspace_enabled=False))))
    with patch.object(routes, "read_workspace_modules", AsyncMock(return_value={})):
        assert [i["name"] for i in (await routes.list_tools(request, MagicMock(), OWNER_B, MagicMock()))["items"]] == ["source.read"]  # type: ignore[arg-type]
        assert [i["name"] for i in (await routes.list_tools(request, MagicMock(), OWNER_A, MagicMock()))["items"]] == ["source.read", name]  # type: ignore[arg-type]
    # invoke_tool builds its allowed set from the same helper
    assert [i.name for i in routes._visible_tools(request, WS_B)] == ["source.read"]  # type: ignore[arg-type]
    assert [i.name for i in routes._visible_tools(request, WS_A)] == ["source.read", name]  # type: ignore[arg-type]


# 4. P3 additions -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_discovery_inserts_carry_workspace() -> None:
    import hashlib
    session = _session()
    sizes = MagicMock()
    sizes.one.return_value = (None, None)
    session.execute = AsyncMock(return_value=sizes)
    connection = SimpleNamespace(revision=1, deployment_profile_hash=None, transport="streamable_http")
    payload = DiscoveryPersist.model_construct(
        connection_revision=1, deployment_profile_hash=None, protocol="p", server_info={},
        schema_set_hash=hashlib.sha256(b"[]").hexdigest(), capabilities=(),
    )
    with patch.object(mcp_repository, "get_connection", AsyncMock(return_value=connection)), \
            patch.object(mcp_repository, "to_discovery_read", return_value=MagicMock()):
        await mcp_repository.persist_discovery(session, uuid4(), payload, scope=OWNER_B)
    assert session.add.call_args.args[0].workspace_id == WS_B


@pytest.mark.asyncio
async def test_replace_connection_grants_inserts_carry_workspace() -> None:
    session = _session()
    connection = SimpleNamespace(revision=1, transport="streamable_http", deployment_profile_hash=None,
                                 draft_check_profile_hash=None)
    session.scalar = AsyncMock(return_value=SimpleNamespace(id=uuid4(), deployment_profile_hash=None))
    capability = SimpleNamespace(id=uuid4(), descriptor_hash="h" * 64)
    session.scalars = AsyncMock(side_effect=[MagicMock(all=lambda: [capability]), MagicMock(all=list)])
    choice = GrantChoice.model_construct(
        capability_id=capability.id, descriptor_hash="h" * 64, purpose="chat", risk=McpRisk.READ_ONLY,
        source_ids=(), destinations=("local",), expires_at=None,
    )
    with patch.object(mcp_repository, "get_connection", AsyncMock(return_value=connection)), \
            patch.object(mcp_repository, "to_grant_read", return_value=MagicMock()):
        await mcp_repository.replace_connection_grants(session, uuid4(), 1, uuid4(), (choice,), scope=OWNER_B)
    assert session.add_all.call_args.args[0][0].workspace_id == WS_B


@pytest.mark.asyncio
async def test_rotate_inbound_client_replacement_carries_workspace_and_predicates() -> None:
    session = _session()
    old = SimpleNamespace(
        revision=1, revoked_at=None, expires_at=datetime(2099, 1, 1, tzinfo=UTC), name="c",
        audience="a", bindings=[], source_ids=[], capabilities=[],
    )
    session.scalar = AsyncMock(return_value=old)
    with patch.object(mcp_repository, "InboundClientIssued"), patch.object(mcp_repository, "to_inbound_read"):
        await mcp_repository.rotate_inbound_client(session, uuid4(), 1, scope=OWNER_B)
    sql = _sql(session.scalar.await_args.args[0])
    assert "mcp_inbound_clients.workspace_id = " in sql and "mcp_inbound_clients.owner_id = " in sql
    assert session.add.call_args.args[0].workspace_id == WS_B


@pytest.mark.asyncio
async def test_revalidate_inbound_principal_select_carries_workspace_and_owner() -> None:
    session = _session()
    assert await mcp_repository.revalidate_inbound_principal(session, _identity()) is False
    sql = _sql(session.scalar.await_args.args[0])
    assert "mcp_inbound_clients.workspace_id = " in sql and "mcp_inbound_clients.owner_id = " in sql


def _route_request(flag: bool = False) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(multi_workspace_enabled=flag), session_factory=MagicMock(),
        mcp_runtime=SimpleNamespace(client=MagicMock(), profile_catalog=MagicMock()),
    )))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["draft_check_route", "discover_route"])
async def test_member_gets_403_not_503_on_draft_check_and_discover(route: str) -> None:
    from modules.tools import mcp_management_routes as routes
    session = _session()
    session.rollback = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await getattr(routes, route)(uuid4(), _route_request(), session, SimpleNamespace(token_hash="t"), MEMBER_A)
    assert exc.value.status_code == 403
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_route_commits_with_locked_access_fence() -> None:
    from modules.tools import mcp_management_routes as routes
    fence = object()
    payload = SimpleNamespace(draft=SimpleNamespace(transport=SimpleNamespace(value="streamable_http")),
                              expected_revision=1)
    request = _route_request()
    request.app.state.settings = SimpleNamespace(
        multi_workspace_enabled=False, connector_credential_encryption_key=SecretStr("k"),
    )
    request.app.state.mcp_runtime.refresh_connection = AsyncMock()
    item = MagicMock()
    item.model_dump.return_value = {}
    with patch.object(routes.repository, "admit", AsyncMock(return_value=fence)) as admit, \
            patch.object(routes.repository, "save_connection", AsyncMock(return_value=item)), \
            patch.object(routes, "commit_with_replay", AsyncMock()) as commit:
        await routes.update_connection_route(
            uuid4(), payload, request, _session(), SimpleNamespace(token_hash="t"), OWNER_A,  # type: ignore[arg-type]
        )
    assert admit.await_args.kwargs["lock"] is True
    assert commit.await_args.kwargs["access_fence"] is fence
