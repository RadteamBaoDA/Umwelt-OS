import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from core.workspaces.schemas import WorkspaceContext
from modules.dashboard import live_map_routes as routes
from modules.dashboard.live_credentials import LiveCredentials


class Session:
    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_event_cache_is_workspace_and_revision_scoped(monkeypatch):
    routes._cache.clear()
    revision = 1
    calls = []
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=revision)

    async def fetch(*args, **kwargs):
        calls.append(1)
        return [], False

    monkeypatch.setattr(routes, "read_access_fence", fence)
    monkeypatch.setattr(routes, "read_live_credentials", credentials)
    monkeypatch.setattr(routes, "require_live_terms", lambda *args: None)
    monkeypatch.setattr(routes, "authenticated_session_ref", lambda request: object())
    monkeypatch.setattr(routes, "PublicationFence", lambda **kwargs: object())
    monkeypatch.setattr(routes, "require_publication_gate", lambda *args: None)
    monkeypatch.setattr(routes.live_map, "fetch_events", fetch)
    for _ in range(2):
        await routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    assert len(calls) == 1
    revision = 2
    await routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    assert len(calls) == 2
    other = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="owner", membership_revision=1)
    await routes.events("usgs", request, Response(), Session(), other, country=None, owner=None, repo=None, cik=None)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_weather_rejects_non_owner_before_fetch():
    workspace = WorkspaceContext(user_id=2, workspace_id=uuid4(), role="member", membership_revision=1)
    with pytest.raises(HTTPException) as exc:
        await routes.weather("wind", 0, 0, 0, None, Session(), workspace)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoked_terms_deny_even_a_cached_result(monkeypatch):
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=1)

    monkeypatch.setattr(routes, "read_access_fence", fence)
    monkeypatch.setattr(routes, "read_live_credentials", credentials)
    routes._cache[(workspace.workspace_id, 1, "usgs", None, None, None, None)] = (float("inf"), {"items": []})
    with pytest.raises(HTTPException) as exc:
        await routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_live_headers_cover_validation_and_unexpected_errors():
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def app(scope, receive, send):
        raise RuntimeError("provider secret and payload")

    middleware = routes.LiveMapHeadersMiddleware(app)
    await middleware({"type": "http", "path": "/api/v1/live-map/events", "method": "GET", "headers": []}, receive, send)
    start, body = messages
    headers = dict(start["headers"])
    assert start["status"] == 503
    assert headers[b"cache-control"] == b"private, no-store"
    assert headers[b"vary"] == b"Cookie, X-Workspace-ID"
    assert b"provider secret" not in body["body"]


@pytest.mark.asyncio
async def test_identical_concurrent_fetches_share_inflight(monkeypatch):
    routes._cache.clear()
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=1)

    async def fetch(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()
        return [], False

    monkeypatch.setattr(routes, "read_access_fence", fence)
    monkeypatch.setattr(routes, "read_live_credentials", credentials)
    monkeypatch.setattr(routes, "require_live_terms", lambda *args: None)
    monkeypatch.setattr(routes, "authenticated_session_ref", lambda request: object())
    monkeypatch.setattr(routes, "PublicationFence", lambda **kwargs: object())
    monkeypatch.setattr(routes, "require_publication_gate", lambda *args: None)
    monkeypatch.setattr(routes.live_map, "fetch_events", fetch)
    def invoke():
        return routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)

    first = asyncio.create_task(invoke())
    await entered.wait()
    second = asyncio.create_task(invoke())
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_revision_change_during_fetch_denies_and_does_not_cache(monkeypatch):
    routes._cache.clear()
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))))
    revisions = iter([1, 2])

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=next(revisions))

    async def fetch(*args, **kwargs):
        return [{"title": "payload"}], False

    monkeypatch.setattr(routes, "read_access_fence", fence)
    monkeypatch.setattr(routes, "read_live_credentials", credentials)
    monkeypatch.setattr(routes, "require_live_terms", lambda *args: None)
    monkeypatch.setattr(routes.live_map, "fetch_events", fetch)
    with pytest.raises(HTTPException) as exc:
        await routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    assert exc.value.status_code == 409
    assert not routes._cache
