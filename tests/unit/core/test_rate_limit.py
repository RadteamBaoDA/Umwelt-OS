"""Request budget: limit, window reset, per-session isolation, Redis outage policy."""

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from core import rate_limit
from core.errors import install_error_handling
from core.rate_limit import require_budget


class _Pipe:
    def __init__(self, store, fail):
        self.store, self.fail, self.key = store, fail, None

    def incr(self, key):
        self.key = key

    def expire(self, *a, **k):
        pass

    async def execute(self):
        if self.fail:
            raise RedisError("down")
        self.store[self.key] = self.store.get(self.key, 0) + 1
        return [self.store[self.key], True]


def _client(fail=False, **settings):
    store: dict = {}
    app = FastAPI()
    install_error_handling(app)
    app.state.settings = SimpleNamespace(**settings)
    app.state.redis = SimpleNamespace(pipeline=lambda transaction=True: _Pipe(store, fail))

    @app.post("/x", dependencies=[Depends(require_budget("t", 2, 60))])
    async def x() -> dict:
        return {}

    return TestClient(app)


def test_limit_hit_and_per_session_isolation():
    c = _client()
    assert [c.post("/x", cookies={"bbd_session": "a"}).status_code for _ in range(3)] == [200, 200, 429]
    r = c.post("/x", cookies={"bbd_session": "a"})
    assert r.headers["Retry-After"] and r.json()["error"]["code"] == "RATE_LIMITED"
    assert c.post("/x", cookies={"bbd_session": "b"}).status_code == 200


def test_window_reset(monkeypatch):
    c = _client()
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])
    for _ in range(3):
        c.post("/x", cookies={"bbd_session": "a"})
    now[0] += 60
    assert c.post("/x", cookies={"bbd_session": "a"}).status_code == 200


def test_override_and_disable():
    c = _client(request_budget_overrides={"t": (1, 60)})
    assert [c.post("/x").status_code for _ in range(2)] == [200, 429]
    assert _client(request_budget_enabled=False).post("/x").status_code == 200


@pytest.mark.parametrize(("open_", "status"), [(False, 503), (True, 200)])
def test_redis_down(open_, status):
    assert _client(fail=True, request_budget_fail_open=open_).post("/x").status_code == status
