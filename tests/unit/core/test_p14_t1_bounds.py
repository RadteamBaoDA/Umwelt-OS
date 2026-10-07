"""P14-T1: DB/Redis bounds, request body limit, realtime batching, CSRF secret guard."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.main import create_app
from core import realtime_routes
from core.body_limit import BodyLimitMiddleware
from core.config import Settings
from core.database import make_session_factory
from core.realtime import ReplayCursor


def _capture(monkeypatch: pytest.MonkeyPatch, **kwargs: int) -> dict:
    from core import database

    seen: dict = {}
    real = database.create_async_engine

    def spy(url, **kw):
        seen.update(kw)
        return real(url, **kw)

    monkeypatch.setattr(database, "create_async_engine", spy)
    engine, _ = make_session_factory("postgresql+asyncpg://u:p@h/db", **kwargs)
    seen["engine"] = engine
    return seen


def test_make_session_factory_applies_pool_and_server_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, pool_size=3, max_overflow=2, statement_timeout_ms=60000, idle_tx_timeout_ms=240000)
    assert (seen["pool_size"], seen["max_overflow"], seen["pool_timeout"], seen["pool_recycle"]) == (3, 2, 5, 1800)
    assert seen["pool_pre_ping"] is True
    assert seen["connect_args"] == {"server_settings": {
        "statement_timeout": "60000", "idle_in_transaction_session_timeout": "240000",
    }}
    pool = seen["engine"].pool
    assert (pool.size(), pool._max_overflow, pool._timeout) == (3, 2, 5)


def test_make_session_factory_skips_zero_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, statement_timeout_ms=0, idle_tx_timeout_ms=240000)
    assert seen["connect_args"] == {"server_settings": {"idle_in_transaction_session_timeout": "240000"}}


def test_redis_client_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(csrf_signing_secret="s"))
    kw = app.state.redis.connection_pool.connection_kwargs
    assert (kw["socket_timeout"], kw["socket_connect_timeout"], kw["health_check_interval"]) == (5, 2, 30)
    assert app.state.redis.connection_pool.max_connections == 100


def _limited_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, default_limit=1000, upload_limit=5000)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    @app.post("/api/v1/documents/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    return app


def test_body_limit_content_length_and_chunked_and_upload() -> None:
    client = TestClient(_limited_app())
    assert client.post("/echo", content=b"x" * 1000).json() == {"n": 1000}
    assert client.post("/echo", content=b"x" * 1001).status_code == 413
    assert client.post("/echo", content=(b"x" * 600 for _ in range(2))).status_code == 413  # chunked, no length
    assert client.post("/api/v1/documents/upload", content=b"x" * 4000).json() == {"n": 4000}
    assert client.post("/api/v1/documents/upload", content=b"x" * 5001).status_code == 413


def test_create_app_applies_body_limit_defaults() -> None:
    app = create_app(Settings(csrf_signing_secret="s"))
    client = TestClient(app)
    assert client.post("/health", content=b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    assert client.post("/api/v1/documents/upload", content=b"x" * (26 * 1024 * 1024 + 1)).status_code == 413


def test_realtime_stream_cap_is_32() -> None:
    assert realtime_routes.MAX_STREAMS_PER_API_PROCESS == 32
    app = create_app(Settings(csrf_signing_secret="s"))
    assert app.state.realtime_connections._value == 32


def test_csrf_secret_required_with_multiple_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="CSRF_SIGNING_SECRET"):
        create_app(Settings(csrf_signing_secret=""))
    create_app(Settings(csrf_signing_secret="set"))
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    create_app(Settings(csrf_signing_secret=""))


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalars(self, _query):
        return SimpleNamespace(all=lambda: _RECORDS)


_EPOCH = uuid4()
_RECORDS = [
    SimpleNamespace(epoch=_EPOCH, sequence=n, event_type="t", payload={"n": n}) for n in (1, 2, 3)
]


def test_realtime_batch_is_one_chunk_with_one_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    head = SimpleNamespace(epoch=_EPOCH, sequence=3, floor_sequence=1)
    calls = 0

    async def fake_head(_s):
        return head

    async def fake_current(_r):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(realtime_routes, "current_head", fake_head)
    monkeypatch.setattr(realtime_routes, "_session_is_current", fake_current)

    async def disconnected() -> bool:
        return False

    request = SimpleNamespace(
        is_disconnected=disconnected,
        app=SimpleNamespace(state=SimpleNamespace(
            session_factory=lambda: _FakeSession(), realtime_connections=asyncio.Semaphore(1),
        )),
    )

    async def run() -> tuple[str, int]:
        cursor = ReplayCursor(epoch=_EPOCH, sequence=0).encode()
        response = await realtime_routes.stream_events(request, cursor=cursor, last_event_id=None)
        chunk = await anext(response.body_iterator)
        before = calls
        await response.body_iterator.aclose()  # type: ignore[attr-defined]
        return chunk, before

    chunk, polls_checked = asyncio.run(run())
    assert chunk.count("event: t") == 3
    assert polls_checked == 2  # one at admission, one for the whole batch
