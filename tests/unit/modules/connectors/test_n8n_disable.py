"""n8n kill switch: ingress guards, wake, reconcile cleanup, workflows listing and callback base."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.workspaces.schemas import WorkspaceContext
from modules.connectors import provisioning, provisioning_routes, public, routes, worker
from modules.connectors.n8n import CALLBACK_BASE
from modules.sources.public import ConnectorStatusRead

WS = uuid4()
OWNER = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
MEMBER = WorkspaceContext(user_id=2, workspace_id=WS, role="member", membership_revision=1)


def _http() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))


@pytest.mark.asyncio
async def test_require_n8n_backend_rejects_when_disabled() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await provisioning.require_n8n_backend(session, uuid4(), n8n_enabled=False)  # type: ignore[arg-type]
    assert exc.value.status_code == 409 and exc.value.detail == "n8n_disabled"
    await provisioning.require_n8n_backend(session, uuid4())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_collector_sync_gets_409_when_disabled() -> None:
    scope = SimpleNamespace(workspace_id=WS)
    with patch.object(routes.ingestion, "resolve_collector_job_scope", AsyncMock(return_value=scope)), \
         patch.object(routes, "read_access_fence", AsyncMock(return_value=object())), \
         patch.object(routes, "_collector_current", AsyncMock()), \
         patch.object(routes.sources, "lock_source", AsyncMock(return_value=object())), \
         patch.object(routes, "n8n_enabled", AsyncMock(return_value=False)), \
         pytest.raises(HTTPException) as exc:
        await routes._collector(MagicMock(), uuid4(), "Bearer tok", multi_workspace_enabled=False)
    assert exc.value.status_code == 409 and exc.value.detail == "n8n_disabled"


@pytest.mark.asyncio
async def test_activate_returns_503_when_disabled() -> None:
    row = SimpleNamespace(desired_revision=1, state="saved_not_active", error_code=None, transition_phase="idle",
                          source_generation=1, execution_backend="n8n")
    source = SimpleNamespace(status="active", generation=1, provider=None)
    payload = SimpleNamespace(expected_revision=1, execution_backend="n8n")
    n8n = SimpleNamespace(enabled=False, api_key="k", webhook_token="t")
    with patch.object(provisioning_routes, "_owner_access", AsyncMock(return_value=object())), \
         patch.object(provisioning_routes, "_source", AsyncMock(return_value=source)), \
         patch.object(provisioning_routes.provisioning, "activation_status", AsyncMock(return_value=row)), \
         patch.object(provisioning_routes.provisioning, "unresolved_credential_error", AsyncMock(return_value=None)), \
         patch.object(provisioning_routes, "_choose_backend", return_value="n8n"), \
         patch.object(provisioning_routes, "_is_fresh", return_value=True), \
         patch.object(provisioning_routes, "resolve_n8n", AsyncMock(return_value=n8n)), \
         pytest.raises(HTTPException) as exc:
        await provisioning_routes.activate_source(uuid4(), payload, MagicMock(), _http(), OWNER)  # type: ignore[arg-type]
    assert exc.value.status_code == 503 and exc.value.detail == "n8n_disabled"


@pytest.mark.asyncio
async def test_wake_is_unavailable_when_disabled() -> None:
    source = SimpleNamespace(status="active", generation=1, type="rss")
    prov = SimpleNamespace(state="active", applied_revision=2, desired_revision=2, source_generation=1)
    session = MagicMock()
    session.rollback = AsyncMock()
    client = MagicMock(side_effect=AssertionError("disabled n8n must never be contacted"))
    n8n = SimpleNamespace(enabled=False, webhook_token="t", service_url="http://n8n:5678")
    with patch.object(public, "_connector_access", AsyncMock(return_value=object())), \
         patch("modules.sources.public.lock_source", AsyncMock(return_value=SimpleNamespace(local_only=False))), \
         patch("modules.sources.public.get_connector_source", AsyncMock(return_value=source)), \
         patch("modules.connectors.provisioning.activation_status", AsyncMock(return_value=prov)), \
         patch.object(public, "require_collection_fence", AsyncMock(return_value=True)), \
         patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)), \
         patch("modules.settings.public.resolve_n8n", AsyncMock(return_value=n8n)), \
         patch("httpx.AsyncClient", client):
        result = await public.wake_packaged_collection(
            session, source_id=uuid4(), source_generation=1, connector_revision=2, settings=MagicMock(),
            scope=OWNER, multi_workspace_enabled=False)
    assert result.outcome == "unavailable"


@pytest.mark.asyncio
async def test_reconcile_discovers_all_workspaces_without_instance_wide_kill_switch() -> None:
    """Discovery cannot borrow one workspace's kill switch; admitted workers apply their own."""
    sid = uuid4()
    statements: list[str] = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def execute(self, _stmt: object) -> MagicMock:
            return MagicMock(all=lambda: [(sid, "slot", uuid4(), "delete")])

        async def scalars(self, stmt: object) -> MagicMock:
            statements.append(str(stmt))
            return MagicMock(all=lambda: [sid])

    drive_credential, drive_workflow = AsyncMock(return_value=1), AsyncMock(return_value=1)
    resume, recover = AsyncMock(return_value=1), AsyncMock(return_value=1)
    ctx = {"settings": SimpleNamespace(multi_workspace_enabled=False), "session_factory": lambda: _Session()}
    with patch.object(worker, "dispatch_github_webhooks", AsyncMock(return_value=0)), \
         patch.object(worker, "_advance_transitions", AsyncMock(return_value=0)), \
         patch("modules.settings.public.resolve_n8n", AsyncMock(side_effect=AssertionError("unscoped n8n settings must not be resolved"))), \
         patch.object(worker, "_cursor_ctx", MagicMock()), patch.object(worker, "_read_cursor", AsyncMock(return_value=None)), \
         patch.object(worker, "_read_slot_cursor", MagicMock(return_value=None)), \
         patch.object(worker, "_write_slot_cursor", MagicMock()), patch.object(worker, "_write_cursor", AsyncMock()), \
         patch.object(worker, "_delete_credential", drive_credential), patch.object(worker, "_drive_workflow", drive_workflow), \
         patch.object(worker, "_resume_activation", resume), patch.object(worker, "_recover_unknown_create", recover):
        await worker.reconcile_connectors(ctx)  # type: ignore[arg-type]
    drive_credential.assert_awaited_once()
    drive_workflow.assert_awaited_once()
    resume.assert_awaited_once()
    recover.assert_awaited_once()
    assert len(statements) == 3 and "workflow_operation" in statements[0]


@pytest.mark.asyncio
async def test_workflows_endpoint_member_gets_403() -> None:
    with pytest.raises(HTTPException) as exc:
        await provisioning_routes.list_n8n_workflows(MagicMock(), _http(), MEMBER)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_workflows_endpoint_is_n8n_only_workspace_scoped_and_bounded() -> None:
    sid, other = uuid4(), uuid4()
    prov = [SimpleNamespace(source_id=sid, workflow_id="wf1", state="active", error_code=None, transition_phase="idle"),
            SimpleNamespace(source_id=other, workflow_id=None, state="queued", error_code=None, transition_phase="idle")]
    seen: list[object] = []

    async def scalars(stmt: object) -> MagicMock:
        seen.append(stmt)
        return MagicMock(all=lambda: prov)

    session = MagicMock()
    session.scalars = scalars
    session.execute = AsyncMock(return_value=MagicMock(tuples=lambda: MagicMock(all=lambda: [(sid, None)])))
    status = {sid: ConnectorStatusRead(sid, "Feed", "rss", None, None, None, None, None)}
    lister = AsyncMock(return_value=status)
    with patch.object(provisioning_routes.sources, "list_connector_status", lister):
        out = await provisioning_routes.list_n8n_workflows(session, _http(), OWNER)  # type: ignore[arg-type]
    # Ids that the Sources owner did not return (other workspace) are dropped.
    assert [(r.source_id, r.source_name, r.workflow_id, r.state) for r in out] == [(sid, "Feed", "wf1", "active")]
    sql = str(seen[0].compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
    assert "execution_backend = 'n8n'" in sql and "LIMIT 200" in sql and WS.hex in sql.replace("-", "")
    assert lister.await_args.kwargs["scope"] is OWNER


def test_every_packaged_workflow_calls_back_to_the_fixed_base() -> None:
    root = Path(__file__).resolve().parents[4] / "infrastructure" / "n8n" / "workflows"
    files = sorted(root.glob("*.json"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        for url in re.findall(r'"url":"(=?https?://[^"]+)"', text):
            assert url.lstrip("=").startswith(CALLBACK_BASE + "/"), f"{path.name}: {url}"
