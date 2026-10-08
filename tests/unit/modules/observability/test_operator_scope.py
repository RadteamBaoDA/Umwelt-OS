"""W2-O contracts: operator flag only from require_owner routes, purge scope first, workspace-paged maintenance, export scope."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from apps.worker import main as worker_main
from core.system import routes as system_routes
from core.workspaces.schemas import InternalJobScope, WorkspaceContext
from modules.export import routes as export_routes
from modules.observability import maintenance, operations, operations_routes
from modules.observability import public as observability
from modules.observability import routes as observability_routes

WS = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=3)


def _request(flag: bool = False) -> SimpleNamespace:
    """Build the minimal request carrying the rollout flag."""
    settings = SimpleNamespace(multi_workspace_enabled=flag)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


@pytest.mark.asyncio
async def test_summaries_forward_operator_flag_to_every_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = []
    for module in (operations.documents, operations.ingestion, operations.entities, operations.sources):
        mock = AsyncMock(return_value={})
        monkeypatch.setattr(module, "observability_quality_summary", mock)
        mocks.append(mock)
    for module in (operations.ingestion, operations.connectors):
        mock = AsyncMock(return_value={})
        monkeypatch.setattr(module, "observability_queue_summary", mock)
        mocks.append(mock)
    await operations.quality_summary(MagicMock(), instance_operator=True)
    await operations.queue_summary(MagicMock(), instance_operator=True)
    for mock in mocks:
        assert mock.await_args.kwargs["instance_operator"] is True


@pytest.mark.asyncio
async def test_operator_routes_pass_literal_true_and_no_client_field(monkeypatch: pytest.MonkeyPatch) -> None:
    quality, queue = AsyncMock(return_value={}), AsyncMock(return_value={})
    monkeypatch.setattr(operations_routes, "quality_summary", quality)
    monkeypatch.setattr(operations_routes, "queue_summary", queue)
    await operations_routes.read_quality(MagicMock(), MagicMock())
    await operations_routes.read_queue(MagicMock(), MagicMock())
    assert quality.await_args.kwargs == {"instance_operator": True}
    assert queue.await_args.kwargs == {"instance_operator": True}
    for route in (operations_routes.read_quality, operations_routes.read_queue, operations_routes.read_run_detail,
                  observability_routes.read_runs):
        assert "instance_operator" not in inspect.signature(route).parameters


@pytest.mark.asyncio
async def test_run_dispatch_gives_only_ingestion_the_operator_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    ingestion, agent = AsyncMock(return_value=[]), AsyncMock(return_value=[])
    monkeypatch.setattr(observability.automations, "list_run_meta", AsyncMock(return_value=[]))
    monkeypatch.setattr(observability.chat, "list_run_meta", AsyncMock(return_value=[]))
    monkeypatch.setattr(observability.ingestion, "list_run_meta", ingestion)
    monkeypatch.setattr(observability.agents, "list_run_meta", agent)
    await observability.list_runs(MagicMock(), limit=5, instance_operator=True)
    assert ingestion.await_args.kwargs == {"instance_operator": True}
    assert agent.await_args.kwargs == {}
    by_id_ing, by_id_agent = AsyncMock(return_value=None), AsyncMock(return_value=None)
    monkeypatch.setattr(observability.ingestion, "get_run_meta_by_id", by_id_ing)
    monkeypatch.setattr(observability.agents, "get_run_meta_by_id", by_id_agent)
    await observability.get_run_by_id(MagicMock(), "ingestion", uuid4(), instance_operator=True)
    await observability.get_run_by_id(MagicMock(), "agent", uuid4(), instance_operator=True)
    assert by_id_ing.await_args.kwargs == {"instance_operator": True}
    assert by_id_agent.await_args.kwargs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["observability", "system"])
async def test_purge_status_resolves_job_scope_before_reading(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    job_scope = InternalJobScope(workspace_id=WS, actor_user_id=5, membership_revision=2)
    order: list[str] = []

    async def resolve_impl(*args: object, **kwargs: object) -> object:
        order.append("resolve")
        return job_scope

    async def read_impl(*args: object, **kwargs: object) -> str:
        order.append("read")
        return "op"

    resolve, read = AsyncMock(side_effect=resolve_impl), AsyncMock(side_effect=read_impl)
    if target == "observability":
        monkeypatch.setattr(operations_routes.sources, "resolve_source_purge_job_scope", resolve)
        monkeypatch.setattr(operations_routes.sources, "read_source_purge_operation", read)
        call = operations_routes.read_source_operation
    else:
        monkeypatch.setattr(system_routes, "resolve_source_purge_job_scope", resolve)
        monkeypatch.setattr(system_routes, "read_source_purge_operation", read)
        call = system_routes.get_operation
    op_id = uuid4()
    assert await call(operation_id=op_id, request=_request(True), session=MagicMock(), _owner=MagicMock()) == "op"
    assert order == ["resolve", "read"]
    assert resolve.await_args.kwargs == {"multi_workspace_enabled": True}
    assert read.await_args.kwargs == {"scope": job_scope, "multi_workspace_enabled": True}
    resolve.side_effect = None
    resolve.return_value = None
    read.reset_mock()
    with pytest.raises(HTTPException) as exc:
        await call(operation_id=op_id, request=_request(), session=MagicMock(), _owner=MagicMock())
    assert exc.value.status_code == 404
    read.assert_not_awaited()


class _Factory:
    """Session factory whose sessions are distinct mocks, so one-session-per-workspace is observable."""

    def __init__(self) -> None:
        self.sessions: list[MagicMock] = []

    def __call__(self) -> _Factory:
        self._current = MagicMock()
        self._current.commit = AsyncMock()
        self._current.rollback = AsyncMock()
        self._current.scalar = AsyncMock(return_value=None)
        self.sessions.append(self._current)
        return self

    async def __aenter__(self) -> MagicMock:
        return self._current

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_maintenance_redacts_per_workspace_with_owner_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    other = uuid4()
    pages: list[tuple[object, ...]] = [(WS, other), ()]

    async def next_page(*args: object, **kwargs: object) -> tuple[object, ...]:
        return pages.pop(0)

    async def owner_for(session: object, workspace_id: object, **kwargs: object) -> WorkspaceContext | None:
        return None if workspace_id == other else OWNER

    monkeypatch.setattr(maintenance.agents, "list_agent_trace_workspace_ids", next_page, raising=False)
    monkeypatch.setattr(maintenance.workspaces, "resolve_workspace_owner_context", owner_for)
    monkeypatch.setattr(maintenance.workspaces, "read_access_fence", AsyncMock())
    monkeypatch.setattr(maintenance.settings_public, "module_is_enabled", AsyncMock(return_value=True))
    retention = AsyncMock(return_value=SimpleNamespace(agent_trace_days=30))
    monkeypatch.setattr(maintenance.settings_public, "read_retention_settings", retention)
    redact = AsyncMock(return_value=4)
    monkeypatch.setattr(maintenance.agents, "redact_expired_agent_traces", redact)
    monkeypatch.setattr(maintenance, "purge_expired_browser_evidence", AsyncMock(return_value=2))
    cursor_write = AsyncMock()
    monkeypatch.setattr(maintenance.worker_cursors, "read_cursor", AsyncMock(return_value=None))
    monkeypatch.setattr(maintenance.worker_cursors, "write_cursor", cursor_write)
    ctx = {"session_factory": _Factory(), "settings": SimpleNamespace(multi_workspace_enabled=False)}
    assert await maintenance.run_retention_maintenance(ctx) == 6
    scope = redact.await_args.kwargs["scope"]
    assert isinstance(scope, InternalJobScope) and scope.workspace_id == WS and scope.actor_user_id == 1
    assert redact.await_args.kwargs["multi_workspace_enabled"] is False
    assert retention.await_args.kwargs["scope"] is scope
    assert redact.await_count == 1  # unavailable lineage workspace skipped, never rebased
    assert cursor_write.await_args.args[2] is None  # drained pass resets the fairness cursor


@pytest.mark.asyncio
async def test_maintenance_skips_denied_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance.workspaces, "resolve_workspace_owner_context", AsyncMock(return_value=OWNER))
    monkeypatch.setattr(maintenance.workspaces, "read_access_fence", AsyncMock(side_effect=HTTPException(409)))
    redact = AsyncMock()
    monkeypatch.setattr(maintenance.agents, "redact_expired_agent_traces", redact)
    factory = _Factory()
    got = await maintenance._redact_workspace_traces(  # type: ignore[arg-type]
        factory, WS, now=datetime.now(UTC), limit=10, multi_workspace_enabled=False,
    )
    assert got == 0
    redact.assert_not_awaited()
    factory.sessions[0].rollback.assert_awaited()


def _export_page(snapshot: object) -> SimpleNamespace:
    """Return an empty single export page."""
    return SimpleNamespace(snapshot_at=snapshot, snapshot_count=0, available=True, omission_reason=None,
                           payload_bytes=0, max_payload_bytes=1, items=[], fences=[], next_cursor=None)


@pytest.mark.asyncio
async def test_export_actor_mismatch_denied_before_any_query() -> None:
    session = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await export_routes._build_export_response(
            export_routes.ExportFormat.json, session, SimpleNamespace(owner_id=2), OWNER, _request(), MagicMock(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403
    assert not session.method_calls


@pytest.mark.asyncio
async def test_export_passes_scope_to_pages_fences_and_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = datetime.now(UTC)
    page = AsyncMock(return_value=_export_page(snap))
    validate = AsyncMock(return_value=SimpleNamespace(valid=True))
    fake = SimpleNamespace(export_page=page, validate_export_fences=validate)
    privacy = AsyncMock(return_value=SimpleNamespace(store_conversation_history=False, persisted=True, updated_at=None))
    monkeypatch.setattr(export_routes.memory_public, "read_export_privacy", privacy)
    meta = {"snapshot_count": 0, "available": False, "privacy_persisted": True, "privacy_updated_at": None}
    await export_routes._validate_final_fences(
        MagicMock(), 1, {"conversations": meta, "x": {**meta, "available": True}},
        {"conversations": (fake, snap, []), "x": (fake, snap, [])}, scope=OWNER, multi_workspace_enabled=True,
    )
    assert privacy.await_args.kwargs == {"scope": OWNER, "multi_workspace_enabled": True}
    assert validate.await_args.kwargs["scope"] is OWNER
    assert validate.await_args.kwargs["multi_workspace_enabled"] is True
    await export_routes._collect_dataset(
        MagicMock(), 1, "x", "x", fake, [0], scope=OWNER, multi_workspace_enabled=True,
    )
    assert page.await_args.kwargs["scope"] is OWNER and page.await_args.kwargs["multi_workspace_enabled"] is True


@pytest.mark.asyncio
async def test_module_gate_uses_build_availability_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(worker_main._JOB_OWNERS, "fake_job", "observability")
    calls: list[str] = []

    async def fake_job(ctx: dict[str, object]) -> str:
        calls.append("ran")
        return "ok"

    guarded = worker_main._gate_module_job(fake_job)
    assert await guarded({}) == "ok"  # no session_factory in ctx: no database read
    monkeypatch.setattr(worker_main, "_BUILD_AVAILABLE", frozenset())
    assert await guarded({}) is None
    assert calls == ["ran"]
