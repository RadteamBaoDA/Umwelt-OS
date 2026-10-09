"""Managed-n8n admission route, token-carrying /sync and /no-changes, and the packaged templates."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from modules.connectors import routes
from modules.connectors.collection_schemas import (
    CollectionAdmissionRead,
    CollectionAdmissionRequest,
    CollectionRequestRef,
    ManagedConnectorReceipt,
    ManagedNoChanges,
)
from modules.connectors.n8n import build_workflow

SOURCE_ID = uuid4()
FENCE = {"source_generation": 2, "connector_revision": 3, "backend_revision": 4}
RECORD = {"provider_id": "a", "content": "x", "observed_at": "2026-01-01T00:00:00Z"}


def _http() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))


@pytest.mark.asyncio
async def test_admission_authenticates_then_admits_with_trigger() -> None:
    scope = object()
    read = CollectionAdmissionRead(request_id=uuid4(), source_id=SOURCE_ID, admission_token=uuid4(), attempt=1)
    session = MagicMock(rollback=AsyncMock())
    admit = AsyncMock(return_value=read)
    payload = CollectionAdmissionRequest(**FENCE, trigger="manual")
    with (patch.object(routes, "_collector", AsyncMock(return_value=("tok", scope, object()))) as collector,
          patch.object(routes.connectors_public, "admit_managed_collection", admit)):
        out = await routes.admit_collection(SOURCE_ID, payload, _http(), session, "Bearer tok")  # type: ignore[arg-type]
    assert out is read
    collector.assert_awaited_once()
    session.rollback.assert_awaited()  # collector locks are released before admission takes its own
    admit.assert_awaited_once_with(session, scope, SOURCE_ID, payload, trigger="manual", multi_workspace_enabled=False)


@pytest.mark.asyncio
async def test_admission_rejects_bad_bearer_and_propagates_busy() -> None:
    session = MagicMock(rollback=AsyncMock())
    payload = CollectionAdmissionRequest(**FENCE)
    assert payload.trigger == "scheduled"
    with (patch.object(routes, "_collector", AsyncMock(side_effect=HTTPException(401))),
          patch.object(routes.connectors_public, "admit_managed_collection", AsyncMock()) as admit,
          pytest.raises(HTTPException) as exc):
        await routes.admit_collection(SOURCE_ID, payload, _http(), session, None)  # type: ignore[arg-type]
    assert exc.value.status_code == 401
    admit.assert_not_awaited()
    with (patch.object(routes, "_collector", AsyncMock(return_value=("t", object(), object()))),
          patch.object(routes.connectors_public, "admit_managed_collection", AsyncMock(side_effect=HTTPException(409))),
          pytest.raises(HTTPException) as exc):
        await routes.admit_collection(SOURCE_ID, payload, _http(), session, "Bearer t")  # type: ignore[arg-type]
    assert exc.value.status_code == 409


def test_admission_schemas_reject_zero_revision_and_missing_token() -> None:
    with pytest.raises(ValidationError):
        CollectionAdmissionRequest(source_generation=0, connector_revision=1, backend_revision=1)
    with pytest.raises(ValidationError):
        ManagedNoChanges(**FENCE)  # no admission -> refused before any handler runs
    with pytest.raises(ValidationError):
        ManagedConnectorReceipt(**FENCE, records=[RECORD])


def _fenced(monkeypatch: pytest.MonkeyPatch, ingestion_fn: str) -> tuple[AsyncMock, MagicMock]:
    session = MagicMock(rollback=AsyncMock())
    sink = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(routes, "_collector", AsyncMock(return_value=("tok", object(), object())))
    monkeypatch.setattr(routes, "_source", AsyncMock(return_value=SimpleNamespace(provider=None)))
    monkeypatch.setattr(routes, "_collector_current", AsyncMock())
    monkeypatch.setattr(routes, "lock_access_fence", AsyncMock())
    monkeypatch.setattr(routes.provisioning, "require_collection_fence", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.registry, "validate", MagicMock())
    monkeypatch.setattr(routes.ingestion, ingestion_fn, sink)
    return sink, session


@pytest.mark.asyncio
async def test_no_changes_forwards_admission_ref_to_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    sink, session = _fenced(monkeypatch, "accept_collection_no_changes")
    request_id, token = uuid4(), uuid4()
    payload = ManagedNoChanges(**FENCE, admission_request_id=request_id, admission_token=token)
    out = await routes.acknowledge_no_changes(SOURCE_ID, payload, _http(), session, "Bearer tok")  # type: ignore[arg-type]
    assert out.status == "no_changes"
    assert sink.await_args.kwargs["request_ref"] == CollectionRequestRef(request_id=request_id, admission_token=token)


@pytest.mark.asyncio
async def test_no_changes_stale_token_is_409_and_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    sink, session = _fenced(monkeypatch, "accept_collection_no_changes")
    sink.side_effect = HTTPException(status_code=409, detail="Collection admission is no longer current")
    payload = ManagedNoChanges(**FENCE, admission_request_id=uuid4(), admission_token=uuid4())
    with pytest.raises(HTTPException) as exc:
        await routes.acknowledge_no_changes(SOURCE_ID, payload, _http(), session, "Bearer tok")  # type: ignore[arg-type]
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_binds_batch_to_request_and_carries_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    sink, session = _fenced(monkeypatch, "receive_connector_batch")
    request_id, token = uuid4(), uuid4()
    payload = ManagedConnectorReceipt(**FENCE, records=[RECORD], admission_request_id=request_id, admission_token=token)
    await routes.receive_connector_batch(SOURCE_ID, payload, _http(), session, "Bearer tok")  # type: ignore[arg-type]
    batch = sink.await_args.args[1]
    assert batch.batch_key == f"connector-request:{request_id}"  # replay of one request cannot mint a second batch
    assert sink.await_args.kwargs["request_ref"] == CollectionRequestRef(request_id=request_id, admission_token=token)


@pytest.mark.parametrize(("name", "source_type"), [("rest.json", "api"), ("rss.json", "rss")])
def test_templates_admit_before_provider_io_and_carry_token(name: str, source_type: str) -> None:
    source = SimpleNamespace(id=SOURCE_ID, type=source_type, provider=None, generation=4, configuration={})
    body = build_workflow(
        source, desired_revision=3, backend_revision=7, workflow_operation_id=uuid4(),
        collector_credential_id="c", manual_credential_id="m", provider_credential_id=None)
    nodes = {n["name"]: n for n in body["nodes"]}
    admission = nodes["Request collection admission"]
    assert admission["parameters"]["url"].endswith(f"/{SOURCE_ID}/collection-admission")
    assert admission["credentials"]["httpHeaderAuth"]["id"] == "c"  # same collector bearer as /sync
    assert "backend_revision:7" in admission["parameters"]["jsonBody"].replace(" ", "")
    for trigger in ("Schedule", "Manual collection"):  # both entries pass admission first
        assert [t["node"] for t in body["connections"][trigger]["main"][0]] == ["Request collection admission"]
    for step in ("Submit acknowledged batch", "Acknowledge no changes"):
        text = json.dumps(nodes[step]["parameters"]["jsonBody"])
        assert "admission_request_id" in text and "admission_token" in text


# --- remaining n8n collection paths: /rss, /crawl, /provider-fetch, /mcp-collect --------------------------------

from modules.connectors.collection_schemas import ManagedCrawlRequest


def test_remaining_requests_refuse_a_missing_admission() -> None:
    with pytest.raises(ValidationError):
        routes.ProviderFetchRequest(**FENCE)
    with pytest.raises(ValidationError):
        routes.McpScheduledCollectionRequest(**FENCE, connection_id=uuid4())
    with pytest.raises(ValidationError):
        ManagedCrawlRequest(source_id=SOURCE_ID, **FENCE, url="https://example.com/")


@pytest.mark.asyncio
async def test_prove_admission_is_409_when_stale_or_replayed() -> None:
    ref = CollectionRequestRef(request_id=uuid4(), admission_token=uuid4())
    kwargs = {"source_id": SOURCE_ID, "source_generation": 2, "connector_revision": 3, "scope": object()}
    with (patch.object(routes.connectors_public, "lock_collection_request_in_uow", AsyncMock(return_value=False)),
          pytest.raises(HTTPException) as exc):
        await routes._prove_admission(MagicMock(), ref, **kwargs)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    with patch.object(routes.connectors_public, "lock_collection_request_in_uow", AsyncMock(return_value=True)) as lock:
        await routes._prove_admission(MagicMock(), ref, **kwargs)  # type: ignore[arg-type]
    assert lock.await_args.args[1] == ref


@pytest.mark.asyncio
async def test_crawl_queues_under_the_admission_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = AsyncMock(return_value=SimpleNamespace(run_id=uuid4()))
    session = MagicMock(rollback=AsyncMock())
    url = "https://example.com/"
    source = SimpleNamespace(type="web", provider=None)
    config = SimpleNamespace(url=url, js_render=False, max_pages=10, max_depth=2, timeout_seconds=60)
    http = _http()
    http.app.state.settings.browser_shared_token = SimpleNamespace(get_secret_value=lambda: "t")
    monkeypatch.setattr(routes, "_collector", AsyncMock(return_value=("tok", object(), object())))
    monkeypatch.setattr(routes, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(routes, "_collector_current", AsyncMock())
    monkeypatch.setattr(routes, "lock_access_fence", AsyncMock())
    monkeypatch.setattr(routes, "validate_public_url", AsyncMock(return_value=url))
    monkeypatch.setattr(routes.provisioning, "require_collection_fence", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.registry, "configuration", MagicMock(return_value=config))
    monkeypatch.setattr(routes.ingestion, "get_source_cursor", AsyncMock(return_value=None))
    monkeypatch.setattr(routes.ingestion, "queue_connector_crawl", sink)
    request_id, token = uuid4(), uuid4()
    payload = ManagedCrawlRequest(source_id=SOURCE_ID, **FENCE, url=url, admission_request_id=request_id, admission_token=token)
    await routes.submit_crawl(SOURCE_ID, payload, session, http, "Bearer tok")  # type: ignore[arg-type]
    assert sink.await_args.kwargs["request_ref"] == CollectionRequestRef(request_id=request_id, admission_token=token)
    sink.side_effect = HTTPException(status_code=409, detail="Collection admission is no longer current")
    with pytest.raises(HTTPException) as exc:
        await routes.submit_crawl(SOURCE_ID, payload, session, http, "Bearer tok")  # type: ignore[arg-type]
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_mcp_collect_proves_admission_before_any_provider_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import modules.connectors.mcp as mcp_collection

    session = MagicMock(rollback=AsyncMock())
    cid = uuid4()
    source = SimpleNamespace(type=mcp_collection.PROVIDER_ID, status="active")
    monkeypatch.setattr(routes, "_mcp_collector", AsyncMock(return_value=("tok", object(), object())))
    monkeypatch.setattr(routes, "_source", AsyncMock(return_value=source))
    monkeypatch.setattr(mcp_collection, "validate", MagicMock(return_value=SimpleNamespace(connection_id=cid)))
    monkeypatch.setattr(routes.provisioning, "require_collection_fence", AsyncMock(return_value=True))
    prove = AsyncMock(side_effect=HTTPException(status_code=409))
    monkeypatch.setattr(routes, "_prove_admission", prove)
    http = _http()
    http.app.state.mcp_runtime = object()
    payload = routes.McpScheduledCollectionRequest(
        **FENCE, connection_id=cid, admission_request_id=uuid4(), admission_token=uuid4())
    with pytest.raises(HTTPException) as exc:
        await routes.collect_mcp_scheduled(SOURCE_ID, payload, session, http, "Bearer tok")  # type: ignore[arg-type]
    assert exc.value.status_code == 409  # not the 503 stub: the stale token stopped it first
    prove.assert_awaited_once()


@pytest.mark.asyncio
async def test_admission_falls_back_to_mcp_bearer_only_on_401() -> None:
    scope, read = object(), CollectionAdmissionRead(request_id=uuid4(), source_id=SOURCE_ID, admission_token=uuid4(), attempt=1)
    session = MagicMock(rollback=AsyncMock())
    payload = CollectionAdmissionRequest(**FENCE)
    with (patch.object(routes, "_collector", AsyncMock(side_effect=HTTPException(401))),
          patch.object(routes, "_mcp_collector", AsyncMock(return_value=("t", scope, object()))) as mcp,
          patch.object(routes.connectors_public, "admit_managed_collection", AsyncMock(return_value=read))):
        assert await routes.admit_collection(SOURCE_ID, payload, _http(), session, "Bearer t") is read  # type: ignore[arg-type]
    mcp.assert_awaited_once()
    with (patch.object(routes, "_collector", AsyncMock(side_effect=HTTPException(409))),
          patch.object(routes, "_mcp_collector", AsyncMock()) as mcp,
          pytest.raises(HTTPException) as exc):
        await routes.admit_collection(SOURCE_ID, payload, _http(), session, "Bearer t")  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    mcp.assert_not_awaited()


@pytest.mark.parametrize(("source_type", "provider"), [
    ("web", None), ("rss", None), ("mcp", None), ("api", "github"), ("rss", "youtube"),
])
def test_every_template_admits_first_and_passes_the_token_on(source_type: str, provider: str | None) -> None:
    source = SimpleNamespace(id=SOURCE_ID, type=source_type, provider=provider, generation=4, configuration={})
    body = build_workflow(
        source, desired_revision=3, backend_revision=7, workflow_operation_id=uuid4(),
        collector_credential_id="c", manual_credential_id="m", provider_credential_id=None)
    nodes = {n["name"]: n for n in body["nodes"]}
    assert "Request collection admission" in nodes
    assert nodes["Request collection admission"]["credentials"]["httpHeaderAuth"]["id"] == "c"
    for trigger in ("Schedule", "Manual collection"):
        assert [t["node"] for t in body["connections"][trigger]["main"][0]] == ["Request collection admission"]
    # Admission leads into the first provider call and nothing reaches I/O around it.
    first = [t["node"] for t in body["connections"]["Request collection admission"]["main"][0]]
    assert len(first) == 1 and first[0] != "Request collection admission"
    text = json.dumps([n["parameters"] for n in body["nodes"] if n["name"] != "Request collection admission"])
    assert "admission_request_id" in text and "admission_token" in text
