import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from core.errors import DomainError
from core.workspaces.schemas import WorkspaceContext
from modules.dashboard import live_map_routes as routes
from modules.dashboard.live_credentials import LiveCredentials


async def never_disconnected():
    return False


class Session:
    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_event_cache_is_workspace_and_revision_scoped(monkeypatch):
    routes._cache.clear()
    revision = 1
    calls = []
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)

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
    with pytest.raises((DomainError, HTTPException)) as exc:
        await routes.weather("wind", 0, 0, 0, None, Session(), workspace)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoked_terms_deny_even_a_cached_result(monkeypatch):
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=1)

    monkeypatch.setattr(routes, "read_access_fence", fence)
    monkeypatch.setattr(routes, "read_live_credentials", credentials)
    routes._cache[(workspace.workspace_id, 1, "usgs", None, None, None, None)] = (float("inf"), {"items": []})
    with pytest.raises((DomainError, HTTPException)) as exc:
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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)
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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)
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
    with pytest.raises((DomainError, HTTPException)) as exc:
        await routes.events("usgs", request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    assert exc.value.status_code == 409
    assert not routes._cache


@pytest.mark.asyncio
async def test_disconnected_client_is_denied_before_quota_or_provider_send():
    async def disconnected():
        return True

    request = SimpleNamespace(is_disconnected=disconnected)
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    with pytest.raises((DomainError, HTTPException)) as error:
        await routes._before_send(request, workspace, object(), 1, "hackernews")
    assert error.value.status_code == 499


@pytest.mark.asyncio
async def test_administrator_can_read_live_events_with_owner_scope(monkeypatch):
    from core.workspaces.schemas import AdminWorkspaceReadContext

    routes._cache.clear()
    scope = AdminWorkspaceReadContext(actor_user_id=1, workspace_id=uuid4(), workspace_owner_user_id=2,
                                      membership_revision=1, configuration_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=True))), is_disconnected=never_disconnected)
    calls = []

    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        calls.append(kwargs['scope'])
        return LiveCredentials(revision=1)

    async def fetch(*args, **kwargs):
        return [], False

    monkeypatch.setattr(routes, 'read_access_fence', fence)
    monkeypatch.setattr(routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(routes, 'require_live_terms', lambda *args: None)
    monkeypatch.setattr(routes, 'authenticated_session_ref', lambda request: object())
    monkeypatch.setattr(routes, 'PublicationFence', lambda **kwargs: object())
    monkeypatch.setattr(routes, 'require_publication_gate', lambda *args: None)
    monkeypatch.setattr(routes.live_map, 'fetch_events', fetch)
    result = await routes.events('usgs', request, Response(), Session(), scope, country=None, owner=None, repo=None, cik=None)
    assert result['items'] == []
    assert calls and all(item.user_id == 2 and item.actor_user_id == 1 for item in calls)


@pytest.mark.asyncio
async def test_disconnect_during_provider_request_cancels_and_drains_it():
    entered, closed = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()

    async def disconnected():
        return closed.is_set()

    async def upstream(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    observer = routes._Observer(SimpleNamespace(is_disconnected=disconnected),
                                WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1), object(), 1)
    async def join():
        async with routes._join_shared(("test", uuid4()), observer, upstream):
            pass

    task = asyncio.create_task(join())
    await entered.wait()
    closed.set()
    with pytest.raises((DomainError, HTTPException)) as error:
        await asyncio.wait_for(task, timeout=2)
    assert error.value.status_code == 499
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_shared_fetch_survives_leader_disconnect_while_another_client_waits(monkeypatch):
    routes._cache.clear()
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    settings = SimpleNamespace(multi_workspace_enabled=False)
    first_closed = asyncio.Event()
    entered, release, second_admitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def first_disconnected():
        return first_closed.is_set()

    first = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)), is_disconnected=first_disconnected)
    second = SimpleNamespace(app=first.app, is_disconnected=never_disconnected)

    async def fence(*args, **kwargs):
        if len(calls):
            second_admitted.set()
        return object()

    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=1)

    async def fetch(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()
        return [{"id": "event"}], False

    monkeypatch.setattr(routes, 'read_access_fence', fence)
    monkeypatch.setattr(routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(routes, 'require_live_terms', lambda *args: None)
    monkeypatch.setattr(routes, 'authenticated_session_ref', lambda request: object())
    monkeypatch.setattr(routes, 'PublicationFence', lambda **kwargs: object())
    monkeypatch.setattr(routes, 'require_publication_gate', lambda *args: None)
    monkeypatch.setattr(routes.live_map, 'fetch_events', fetch)
    def invoke(request):
        return routes.events('hackernews', request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)

    leader = asyncio.create_task(invoke(first))
    await entered.wait()
    follower = asyncio.create_task(invoke(second))
    await second_admitted.wait()
    await asyncio.sleep(0.05)
    first_closed.set()
    await asyncio.sleep(0.3)
    release.set()
    results = await asyncio.gather(leader, follower, return_exceptions=True)
    assert isinstance(results[1], dict) and results[1]['items'][0]['id'] == 'event'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_shared_send_uses_a_live_authorized_observer(monkeypatch):
    async def disconnected():
        return True

    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    gone = routes._Observer(SimpleNamespace(is_disconnected=disconnected), workspace, object(), 1)
    active = routes._Observer(SimpleNamespace(is_disconnected=never_disconnected), workspace, object(), 1)
    job = routes._SharedJob(observers={gone, active})
    admitted = []

    async def before_send(request, scope, fence, revision, provider):
        admitted.append(request)

    monkeypatch.setattr(routes, '_before_send', before_send)
    await routes._shared_before_send(job, 'hackernews')
    assert admitted == [active.request]


@pytest.mark.asyncio
async def test_cancelling_one_shared_waiter_does_not_cancel_peer():
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    first = routes._Observer(SimpleNamespace(is_disconnected=never_disconnected), workspace, object(), 1)
    second = routes._Observer(SimpleNamespace(is_disconnected=never_disconnected), workspace, object(), 1)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def collect(job):
        calls.append(1)
        entered.set()
        await finish.wait()
        return 'ok'

    key = ('test', uuid4())
    async def join(observer):
        async with routes._join_shared(key, observer, collect) as result:
            return result

    one = asyncio.create_task(join(first))
    await entered.wait()
    two = asyncio.create_task(join(second))
    await asyncio.sleep(0)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    finish.set()
    assert await two == 'ok'
    assert calls == [1] and key not in routes._jobs


@pytest.mark.asyncio
async def test_new_waiter_joins_completed_fetch_during_first_publication(monkeypatch):
    routes._cache.clear()
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    first_session, second_session = Session(), Session()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)
    revalidating, release = asyncio.Event(), asyncio.Event()
    reads = 0
    calls = []

    async def fence(*args, **kwargs):
        return object()

    async def credentials(session, **kwargs):
        nonlocal reads
        if session is first_session:
            reads += 1
            if reads == 2:
                revalidating.set()
                await release.wait()
        return LiveCredentials(revision=1)

    async def fetch(*args, **kwargs):
        calls.append(1)
        return [], False

    monkeypatch.setattr(routes, 'read_access_fence', fence)
    monkeypatch.setattr(routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(routes, 'require_live_terms', lambda *args: None)
    monkeypatch.setattr(routes, 'authenticated_session_ref', lambda request: object())
    monkeypatch.setattr(routes, 'PublicationFence', lambda **kwargs: object())
    monkeypatch.setattr(routes, 'require_publication_gate', lambda *args: None)
    monkeypatch.setattr(routes.live_map, 'fetch_events', fetch)
    def invoke(session):
        return routes.events('hackernews', request, Response(), session, workspace, country=None, owner=None, repo=None, cik=None)

    first = asyncio.create_task(invoke(first_session))
    await revalidating.wait()
    second = asyncio.create_task(invoke(second_session))
    await asyncio.wait_for(second, timeout=2)
    release.set()
    await first
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_shared_send_considers_observer_joining_during_authorization(monkeypatch):
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    first = routes._Observer(SimpleNamespace(is_disconnected=never_disconnected), workspace, object(), 1)
    second = routes._Observer(SimpleNamespace(is_disconnected=never_disconnected), workspace, object(), 1)
    job = routes._SharedJob(observers={first})
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = []

    async def before_send(request, scope, fence, revision, provider):
        if request is first.request:
            entered.set()
            await release.wait()
            raise HTTPException(409, 'live_map_configuration_changed')
        accepted.append(request)

    monkeypatch.setattr(routes, '_before_send', before_send)
    task = asyncio.create_task(routes._shared_before_send(job, 'hackernews'))
    await entered.wait()
    job.observers.add(second)
    release.set()
    await task
    assert accepted == [second.request]
    job.observers.remove(second)
    with pytest.raises((DomainError, HTTPException)) as error:
        await routes._shared_before_send(job, 'hackernews')
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_disconnect_during_final_revalidation_cancels_without_caching(monkeypatch):
    routes._cache.clear()
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
    closed, entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = 0

    async def disconnected():
        return closed.is_set()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=disconnected)
    async def fence(*args, **kwargs):
        return object()

    async def credentials(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return LiveCredentials(revision=1)

    async def fetch(*args, **kwargs):
        return [{'id': 'event'}], False

    monkeypatch.setattr(routes, 'read_access_fence', fence)
    monkeypatch.setattr(routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(routes, 'require_live_terms', lambda *args: None)
    monkeypatch.setattr(routes.live_map, 'fetch_events', fetch)
    task = asyncio.create_task(routes.events('usgs', request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None))
    await entered.wait()
    closed.set()
    with pytest.raises((DomainError, HTTPException)) as error:
        await asyncio.wait_for(task, timeout=2)
    assert error.value.status_code == 499 and cancelled.is_set()
    assert not routes._cache


@pytest.mark.asyncio
async def test_shared_waiters_evict_only_one_full_cache_entry(monkeypatch):
    routes._cache.clear()
    for i in range(128):
        routes._cache[('old', i)] = (float('inf'), {'items': []})
    workspace = WorkspaceContext(user_id=1, workspace_id=uuid4(), role='owner', membership_revision=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(multi_workspace_enabled=False))), is_disconnected=never_disconnected)
    entered, release = asyncio.Event(), asyncio.Event()
    async def fence(*args, **kwargs):
        return object()
    async def credentials(*args, **kwargs):
        return LiveCredentials(revision=1)
    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return [], False
    monkeypatch.setattr(routes, 'read_access_fence', fence)
    monkeypatch.setattr(routes, 'read_live_credentials', credentials)
    monkeypatch.setattr(routes, 'require_live_terms', lambda *args: None)
    monkeypatch.setattr(routes, 'authenticated_session_ref', lambda request: object())
    monkeypatch.setattr(routes, 'PublicationFence', lambda **kwargs: object())
    monkeypatch.setattr(routes, 'require_publication_gate', lambda *args: None)
    monkeypatch.setattr(routes.live_map, 'fetch_events', fetch)
    def invoke():
        return routes.events('usgs', request, Response(), Session(), workspace, country=None, owner=None, repo=None, cik=None)
    first = asyncio.create_task(invoke())
    await entered.wait()
    second = asyncio.create_task(invoke())
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)
    assert len(routes._cache) == 128
    assert sum(key[0] == 'old' for key in routes._cache) == 127
