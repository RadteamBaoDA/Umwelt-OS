"""Workspace-authorized, transient reads of fixed live-map providers."""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.auth.public import authenticated_session_ref
from core.database import get_session
from core.publication import require_publication_gate
from core.workspaces.dependencies import require_workspace_observer_read
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import AdminWorkspaceReadContext, PublicationFence, WorkspaceContext
from modules.connectors import live_map
from modules.connectors.public import ProviderRateLimited
from modules.dashboard.live_credentials import read_live_credentials, require_live_terms

router = APIRouter(prefix="/api/v1/live-map", tags=["live-map"])
Session = Annotated[AsyncSession, Depends(get_session)]
Workspace = Annotated[WorkspaceContext | AdminWorkspaceReadContext, Depends(require_workspace_observer_read)]
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_tiles: dict[tuple[Any, ...], tuple[float, bytes]] = {}
_next_send: dict[str, float] = {}
_recent: dict[str, deque[float]] = {}
_lock = asyncio.Lock()
_parallel = asyncio.Semaphore(4)
_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie, X-Workspace-ID"}


async def _while_connected[T](request: Request, operation: Coroutine[Any, Any, T]) -> T:
    """Cancel and drain final revalidation if this client's request disconnects."""
    async def watch() -> None:
        """Watch only this request while its final database work is pending."""
        while not await request.is_disconnected():
            await asyncio.sleep(0.2)

    work, watcher = asyncio.create_task(operation), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
            raise HTTPException(499, "client_disconnected", headers=_HEADERS)
        return await work
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


@dataclass(eq=False, slots=True)
class _Observer:
    """One independently authorized client waiting for a shared transient fetch."""
    request: Request
    workspace: WorkspaceContext | AdminWorkspaceReadContext
    fence: Any
    revision: int


@dataclass(slots=True)
class _SharedJob:
    """In-memory provider task with a bounded set of interested client sessions."""
    observers: set[_Observer] = field(default_factory=set)
    task: asyncio.Task[Any] | None = None


_jobs: dict[tuple[Any, ...], _SharedJob] = {}


async def _shared_before_send(job: _SharedJob, provider: str) -> None:
    """Admit one connected observer's exact session and revision before each upstream send."""
    attempted: set[_Observer] = set()
    last_error: HTTPException | None = None
    while candidates := job.observers - attempted:
        observer = next(iter(candidates))
        attempted.add(observer)
        if observer not in job.observers or await observer.request.is_disconnected():
            continue
        try:
            await _before_send(observer.request, observer.workspace, observer.fence, observer.revision, provider)
            # A cancelled waiter may leave while its database fence is being checked.
            if observer in job.observers and not await observer.request.is_disconnected():
                return
        except HTTPException as exc:
            if exc.status_code not in {401, 403, 404, 409, 499}:
                raise
            if exc.status_code != 499:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise HTTPException(499, "client_disconnected", headers=_HEADERS)


@asynccontextmanager
async def _join_shared[T](key: tuple[Any, ...], observer: _Observer,
                          factory: Callable[[_SharedJob], Coroutine[Any, Any, T]]) -> AsyncIterator[T]:
    """Share one fetch; disconnecting or cancelling a waiter never cancels its peers' work.

    No await occurs while registering/removing an observer, so one event loop serializes
    ownership changes. The final observer cancels and drains unfinished work.
    """
    job = _jobs.get(key)
    if job is None:
        if len(_jobs) >= 128:
            raise HTTPException(429, "provider_rate_limited", headers=_HEADERS)
        job = _SharedJob()
        _jobs[key] = job
    if len(job.observers) >= 128:
        raise HTTPException(429, "provider_rate_limited", headers=_HEADERS)
    job.observers.add(observer)
    if job.task is None:
        job.task = asyncio.create_task(factory(job))
    work = job.task

    async def watch() -> None:
        """Wait for this particular client to disconnect without consuming another client's receive channel."""
        while not await observer.request.is_disconnected():
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
            raise HTTPException(499, "client_disconnected", headers=_HEADERS)
        value = await asyncio.shield(work)
        # Final publication owns its own watcher; never poll the same receive channel twice.
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        yield value
    finally:
        watcher.cancel()
        job.observers.discard(observer)
        last = not job.observers
        if last:
            if _jobs.get(key) is job:
                _jobs.pop(key)
            if not work.done():
                work.cancel()
        await asyncio.gather(watcher, *((work,) if last else ()), return_exceptions=True)


class LiveMapHeadersMiddleware:
    """Add private cache and workspace variation headers to live-map success and error responses."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the existing direct-send ASGI stack without buffering responses."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set headers at response start, including framework validation failures."""
        if scope["type"] != "http" or not (scope["path"] == "/api/v1/live-map" or scope["path"].startswith("/api/v1/live-map/")):
            await self.app(scope, receive, send)
            return

        started = False

        async def send_private(message: Message) -> None:
            """Preserve existing Vary values while adding workspace and cookie selection."""
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                vary = [value.strip() for value in headers.get("Vary", "").split(",") if value.strip()]
                if "*" not in vary:
                    for name in ("Cookie", "X-Workspace-ID"):
                        if name.lower() not in {value.lower() for value in vary}:
                            vary.append(name)
                headers["Vary"] = ", ".join(vary)
            await send(message)

        try:
            await self.app(scope, receive, send_private)
        except Exception:
            if started:
                raise
            # Unexpected provider/parser errors must not echo or log upstream content.
            await JSONResponse({"error": {"code": "LIVE_MAP_UNAVAILABLE", "message": "Live map unavailable", "details": {}}}, status_code=503)(scope, receive, send_private)


async def _admit(provider: str) -> None:
    """Enforce process-local provider pacing and any bounded provider backoff."""
    async with _lock:
        now = time.monotonic()
        if _next_send.get(provider, 0) > now:
            raise HTTPException(429, "provider_rate_limited", headers=_HEADERS)
        recent = _recent.setdefault(provider, deque())
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= ({"openweather": 60, "hackernews": 32}.get(provider, 10)):
            raise HTTPException(429, "provider_rate_limited", headers=_HEADERS)
        recent.append(now)


async def _before_send(request: Request, workspace: WorkspaceContext | AdminWorkspaceReadContext, fence: Any, revision: int, provider: str) -> None:
    """Stop disconnected requests and revalidate the exact session and workspace before I/O."""
    if await request.is_disconnected():
        raise HTTPException(499, "client_disconnected", headers=_HEADERS)
    settings = request.app.state.settings
    async with request.app.state.session_factory() as session:
        await lock_access_fence(
            session, scope=workspace, expected=fence, multi_workspace_enabled=settings.multi_workspace_enabled,
            auth_sessions=(authenticated_session_ref(request),),
        )
        current = await read_live_credentials(session, scope=workspace, settings=settings, multi_workspace_enabled=settings.multi_workspace_enabled)
        if current.revision != revision:
            raise HTTPException(409, "live_map_configuration_changed", headers=_HEADERS)
        require_live_terms(current, provider)
        try:
            await live_map.debit_live_send(session, provider=provider, workspace_id=workspace.workspace_id)
        except live_map.QuotaDeferred:
            raise HTTPException(429, "provider_rate_limited", headers=_HEADERS) from None
        except ValueError:
            raise HTTPException(503, "provider_quota_unavailable", headers=_HEADERS) from None
        await session.commit()
    await _admit(provider)
    if await request.is_disconnected():
        raise HTTPException(499, "client_disconnected", headers=_HEADERS)


async def _backoff(request: Request, provider: str, until: datetime) -> None:
    """Persist provider-directed retry timing for all API processes sharing the quota ledger."""
    async with request.app.state.session_factory() as session:
        await live_map.defer_live_provider(session, provider=provider, until=until)
        await session.commit()


@router.get("/events")
async def events(
    provider: Annotated[str, Query(max_length=32)], request: Request, response: Response,
    session: Session, workspace: Workspace,
    country: Annotated[str | None, Query(max_length=3)] = None,
    owner: Annotated[str | None, Query(max_length=39)] = None,
    repo: Annotated[str | None, Query(max_length=100)] = None,
    cik: Annotated[str | None, Query(max_length=10)] = None,
) -> dict[str, Any]:
    """Fetch bounded provider records on demand and publish only after workspace/session revalidation."""
    response.headers.update(_HEADERS)
    if not isinstance(workspace, AdminWorkspaceReadContext) and (not isinstance(workspace, WorkspaceContext) or workspace.role != "owner"):
        raise HTTPException(404, "Workspace not found", headers=_HEADERS)
    if provider not in live_map.PROVIDERS:
        raise HTTPException(422, "provider_invalid", headers=_HEADERS)
    settings = request.app.state.settings
    gate = settings.multi_workspace_enabled
    fence = await read_access_fence(session, scope=workspace, multi_workspace_enabled=gate)
    credentials = await read_live_credentials(session, scope=workspace, settings=settings, multi_workspace_enabled=gate)
    require_live_terms(credentials, provider)
    await session.rollback()
    key = (workspace.workspace_id, credentials.revision, provider, country, owner, repo, cik)
    async def publish(result: dict[str, Any], *, fetched: bool) -> dict[str, Any]:
        """Revalidate this client's access and cache only results matching the captured revision."""
        current = await read_live_credentials(session, scope=workspace, settings=settings, multi_workspace_enabled=gate)
        if current.revision != credentials.revision:
            raise HTTPException(409, "live_map_configuration_changed", headers=_HEADERS)
        require_live_terms(current, provider)
        await session.rollback()
        if await request.is_disconnected():
            raise HTTPException(499, "client_disconnected", headers=_HEADERS)
        if fetched:
            async with _lock:
                if key not in _cache and len(_cache) >= 128:
                    _cache.pop(next(iter(_cache)))
                _cache[key] = (time.monotonic() + live_map.live_cache_seconds(provider), result)
        require_publication_gate(request, PublicationFence(scope=workspace, access_fence=fence,
                                 auth_session=authenticated_session_ref(request), grants=()))
        return result

    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return await _while_connected(request, publish(cached[1], fetched=False))

    async def collect(job: _SharedJob) -> dict[str, Any]:
        """Fetch bounded events on behalf of a currently authorized observer."""
        async def before_send() -> None:
            """Authorize a connected observer immediately before each physical send."""
            await _shared_before_send(job, provider)

        async with _parallel:
            items, truncated = await live_map.fetch_events(
                provider, country=country, owner=owner, repo=repo, cik=cik,
                secret=credentials.firms_key if provider == "firms" else None,
                contact=credentials.sec_contact_email, before_send=before_send,
            )
        return {"items": items, "fetched_at": datetime.now(UTC).isoformat(), "truncated": truncated}

    try:
        async with _join_shared(("events", *key), _Observer(request, workspace, fence, credentials.revision), collect) as result:
            return await _while_connected(request, publish(result, fetched=True))
    except ProviderRateLimited as exc:
        await _backoff(request, provider, exc.next_eligible_at)
        async with _lock:
            _next_send[provider] = time.monotonic() + min(3600, max(1, (exc.next_eligible_at - datetime.now(UTC)).total_seconds()))
        raise HTTPException(429, "provider_rate_limited", headers=_HEADERS) from None
    except ValueError as exc:
        detail = str(exc) if str(exc) in {"provider_scope_invalid", "provider_temporarily_unavailable", "contact_required"} else "provider_response_invalid"
        raise HTTPException(422 if detail in {"provider_scope_invalid", "contact_required"} else 503, detail, headers=_HEADERS) from None
    except (httpx.HTTPError, OSError, TimeoutError, KeyError, IndexError, TypeError, live_map.CollectionIncomplete, live_map.UnsafeDestination, live_map.ProviderHttpError):
        raise HTTPException(503, "provider_unavailable", headers=_HEADERS) from None


@router.get("/weather/{layer}/{z}/{x}/{y}")
async def weather(layer: str, z: int, x: int, y: int, request: Request, session: Session, workspace: Workspace) -> Response:
    """Proxy a bounded weather tile under the same workspace and revision fences as events."""
    if not isinstance(workspace, AdminWorkspaceReadContext) and (not isinstance(workspace, WorkspaceContext) or workspace.role != "owner"):
        raise HTTPException(404, "Workspace not found", headers=_HEADERS)
    if layer not in {"temperature", "precipitation", "wind", "clouds"} or not 0 <= z <= 18 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(422, "weather_tile_invalid", headers=_HEADERS)
    settings = request.app.state.settings
    gate = settings.multi_workspace_enabled
    fence = await read_access_fence(session, scope=workspace, multi_workspace_enabled=gate)
    credentials = await read_live_credentials(session, scope=workspace, settings=settings, multi_workspace_enabled=gate)
    require_live_terms(credentials, "openweather")
    await session.rollback()
    if not credentials.openweather_key:
        raise HTTPException(422, "weather_key_required", headers=_HEADERS)

    key = (workspace.workspace_id, credentials.revision, layer, z, x, y)

    async def publish(body: bytes, *, fetched: bool) -> Response:
        """Revalidate this client and cache the tile only under the captured settings revision."""
        current = await read_live_credentials(session, scope=workspace, settings=settings, multi_workspace_enabled=gate)
        if current.revision != credentials.revision:
            raise HTTPException(409, "live_map_configuration_changed", headers=_HEADERS)
        require_live_terms(current, "openweather")
        await session.rollback()
        if await request.is_disconnected():
            raise HTTPException(499, "client_disconnected", headers=_HEADERS)
        if fetched:
            async with _lock:
                if key not in _tiles and len(_tiles) >= 32:
                    _tiles.pop(next(iter(_tiles)))
                _tiles[key] = (time.monotonic() + live_map.live_cache_seconds("openweather"), body)
        require_publication_gate(request, PublicationFence(scope=workspace, access_fence=fence,
                                 auth_session=authenticated_session_ref(request), grants=()))
        return Response(body, media_type="image/png", headers=_HEADERS)

    cached = _tiles.get(key)
    if cached and cached[0] > time.monotonic():
        return await _while_connected(request, publish(cached[1], fetched=False))

    async def collect(job: _SharedJob) -> bytes:
        """Fetch one bounded weather tile on behalf of a currently authorized observer."""
        async def before_send() -> None:
            """Authorize a connected observer immediately before the tile request."""
            await _shared_before_send(job, "openweather")

        async with _parallel:
            return await live_map.fetch_weather(layer, z, x, y, credentials.openweather_key, before_send=before_send)

    try:
        async with _join_shared(("weather", *key), _Observer(request, workspace, fence, credentials.revision), collect) as body:
            return await _while_connected(request, publish(body, fetched=True))
    except ProviderRateLimited as exc:
        await _backoff(request, "openweather", exc.next_eligible_at)
        async with _lock:
            _next_send["openweather"] = time.monotonic() + min(3600, max(1, (exc.next_eligible_at - datetime.now(UTC)).total_seconds()))
        raise HTTPException(429, "provider_rate_limited", headers=_HEADERS) from None
    except (ValueError, httpx.HTTPError, OSError, TimeoutError, live_map.CollectionIncomplete, live_map.UnsafeDestination, live_map.ProviderHttpError):
        raise HTTPException(503, "weather_unavailable", headers=_HEADERS) from None
