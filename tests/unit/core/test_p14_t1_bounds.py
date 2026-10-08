"""P14-T1: DB/Redis bounds, request body limit, realtime batching, CSRF secret guard."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from apps.api.main import create_app
from core import realtime_routes
from core.body_limit import BodyLimitMiddleware
from core.config import Settings
from core.database import make_session_factory
from core.errors import install_error_handling
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


class Item(BaseModel):
    text: str


def _limited_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, default_limit=1000, upload_limit=5000)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    @app.post("/api/v1/documents/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    @app.post("/model")
    async def model(item: Item) -> dict[str, int]:
        return {"n": len(item.text)}

    @app.post("/form")
    async def form(name: Annotated[str, Form()], file: Annotated[UploadFile, File()]) -> dict[str, int]:
        return {"n": len(await file.read())}

    return app


def _chunks(total: int, size: int = 600):
    for _ in range(total // size + 1):
        yield b"x" * size


def test_body_limit_413_shape_on_model_and_form_routes() -> None:
    client = TestClient(_limited_app())
    expected = {"detail": "Request body too large"}
    big = json.dumps({"text": "a" * 1500})
    jh = {"content-type": "application/json"}
    r = client.post("/model", content=big, headers=jh)  # content-length path
    assert (r.status_code, r.json()) == (413, expected)
    r = client.post("/model", content=(big[i:i + 600].encode() for i in range(0, len(big), 600)), headers=jh)  # chunked
    assert (r.status_code, r.json()) == (413, expected)
    r = client.post("/form", data={"name": "n"}, files={"file": ("f.txt", b"x" * 1500)})
    assert (r.status_code, r.json()) == (413, expected)
    boundary = "b0undary"
    def multipart():
        crlf = chr(13) + chr(10)
        yield f'--{boundary}{crlf}Content-Disposition: form-data; name="name"{crlf}{crlf}n{crlf}'.encode()
        yield f'--{boundary}{crlf}Content-Disposition: form-data; name="file"; filename="f.txt"{crlf}{crlf}'.encode()
        yield from _chunks(1500)
        yield f"{crlf}--{boundary}--{crlf}".encode()
    r = client.post("/form", content=multipart(), headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert (r.status_code, r.json()) == (413, expected)
    ok = client.post("/form", data={"name": "n"}, files={"file": ("f.txt", b"x" * 100)})
    assert ok.json() == {"n": 100}


def test_body_limit_passes_4_5_mib_on_normal_route() -> None:
    app = create_app(Settings(csrf_signing_secret="s"))

    @app.post("/p14-echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    size = 4 * 1024 * 1024 + 512 * 1024
    assert TestClient(app).post("/p14-echo", content=b"x" * size).json() == {"n": size}


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
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    with pytest.raises(RuntimeError, match="UVICORN_WORKERS"):
        create_app(Settings(csrf_signing_secret=""))


class _FakeSession:
    async def scalars(self, _query):
        return SimpleNamespace(all=lambda: _RECORDS)

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


_EPOCH = uuid4()
_RECORDS = [
    SimpleNamespace(epoch=_EPOCH, sequence=n, event_type="t", payload={"n": n}) for n in (1, 2, 3)
]


def test_realtime_replay_page_is_one_chunk_with_one_fence_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    head = SimpleNamespace(epoch=_EPOCH, sequence=3, floor_sequence=1)
    locks = 0

    async def fake_head(_s, **_kw):
        return head

    async def fake_lock(_s, **_kw):
        nonlocal locks
        locks += 1

    monkeypatch.setattr(realtime_routes, "current_head", fake_head)
    monkeypatch.setattr(realtime_routes.workspaces, "lock_access_fence", fake_lock)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_factory=lambda: _FakeSession(), settings=SimpleNamespace(multi_workspace_enabled=False),
    )))

    page = asyncio.run(realtime_routes._read_replay_page(
        request, workspace=SimpleNamespace(workspace_id=uuid4(), user_id=1), fence=SimpleNamespace(),
        auth_session=SimpleNamespace(), position=ReplayCursor(epoch=_EPOCH, sequence=0),
    ))
    assert page.reason is None
    assert len(page.messages) == 1  # one guarded send per page, not per record
    cursor, chunk = page.messages[0]
    assert chunk.count("event: t") == 3
    assert cursor == ReplayCursor(epoch=_EPOCH, sequence=3)
    assert locks == 1


def test_web_concurrency_non_integer_is_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "auto")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY must be a positive integer"):
        create_app(Settings(csrf_signing_secret="s"))


def test_stream_permit_released_when_response_never_starts() -> None:
    sem = asyncio.Semaphore(1)

    async def run() -> int:
        await sem.acquire()

        async def gen():
            yield "x"

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_m):
            raise OSError("client gone")

        response = realtime_routes._PermitResponse(gen(), asyncio.Event(), sem)
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        return sem._value

    assert asyncio.run(run()) == 1


@pytest.mark.parametrize(("sqlstate", "status"), [("57014", 503), ("55P03", 503), ("23505", 500)])
def test_db_timeouts_map_to_503_only(sqlstate: str, status: int) -> None:
    from sqlalchemy.exc import DBAPIError

    app = FastAPI()
    install_error_handling(app)

    @app.get("/boom")
    async def boom() -> None:
        raise DBAPIError("SELECT 1", {}, SimpleNamespace(sqlstate=sqlstate))  # type: ignore[arg-type]

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == status
    assert (r.headers.get("Retry-After") == "5") == (status == 503)
