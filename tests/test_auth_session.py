import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import create_app
from core.auth import routes as auth_routes
from core.auth.models import AuthSession, Owner
from core.auth.routes import get_auth_redis
from core.auth.service import hash_password
from core.config import Settings
from core.database import get_session


class MemoryAuthStore:
    def __init__(self) -> None:
        self.owner = Owner(id=1, password_hash=hash_password("test-owner-password-42"))
        self.sessions: dict[str, AuthSession] = {}
        self.pending = None
        self.lock = asyncio.Lock()


class MemorySession:
    def __init__(self, store: MemoryAuthStore) -> None:
        self.store = store
        self.pending = None

    async def scalar(self, statement):
        str_stmt = str(statement)
        if "auth_session" in str_stmt.lower():
            return next(iter(self.store.sessions.values()), None)
        return self.store.owner

    async def get(self, model, key):
        if model is Owner:
            return self.store.owner if key == 1 else None
        return self.store.sessions.get(key)

    def add(self, value) -> None:
        self.pending = value

    async def commit(self) -> None:
        async with self.store.lock:
            if isinstance(self.pending, AuthSession):
                self.store.sessions[self.pending.token_hash] = self.pending

    async def execute(self, _statement):
        return object()

    async def delete(self, value) -> None:
        self.store.sessions.pop(value.token_hash, None)

    async def rollback(self) -> None:
        return None


class MemoryRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.keys: list[str] = []

    def pipeline(self, transaction=True):
        return self

    async def ping(self):
        return True

    async def get(self, _key):
        return None

    def incr(self, key):
        self.keys.append(key)
        return self

    def expire(self, key, seconds, nx=True):
        return self

    async def execute(self):
        result = []
        for key in self.keys:
            self.counts[key] = self.counts.get(key, 0) + 1
            result.extend((self.counts[key], True))
        self.keys.clear()
        return result


def test_csrf_cookie_expires_on_server(monkeypatch) -> None:
    settings = Settings(csrf_signing_secret="test-csrf-signing-secret")
    monkeypatch.setattr(auth_routes.time, "time", lambda: 1)
    token, cookie = auth_routes._new_csrf(settings)
    assert auth_routes._valid_csrf(cookie, token, settings)
    monkeypatch.setattr(auth_routes.time, "time", lambda: 602)
    assert not auth_routes._valid_csrf(cookie, token, settings)


@pytest.mark.asyncio
async def test_login_requires_csrf_and_rotates_session_tokens() -> None:
    store = MemoryAuthStore()
    app = create_app(Settings(public_origin="http://localhost:3000", csrf_signing_secret="test-csrf-signing-secret"))

    async def session_override():
        yield MemorySession(store)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_auth_redis] = lambda: MemoryRedis()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:3000"
    ) as client:
        origin = "http://localhost:3000"
        csrf_response = await client.get("/api/v1/auth/csrf")
        assert csrf_response.headers["cache-control"] == "no-store"
        csrf_token = csrf_response.json()["csrfToken"]
        cookie_before = client.cookies.get("bbd_csrf")
        missing_origin = await client.post(
            "/api/v1/auth/login",
            headers={"X-CSRF-Token": csrf_token},
            json={"password": "not-a-real-password"},
        )
        assert missing_origin.status_code == 403
        wrong_password = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf_token},
            json={"password": "not-a-real-password"},
        )
        assert wrong_password.status_code == 401
        assert "not-a-real-password" not in wrong_password.text
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf_token},
            json={"password": "test-owner-password-42"},
        )
        assert login.status_code == 200
        session_cookie = client.cookies.get("bbd_session")
        assert session_cookie and session_cookie != cookie_before
        assert "bbd_session" not in str(login.json())
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=lax" in login.headers["set-cookie"]

        health = await client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["components"]["model_gateway"]["status"] == "unconfigured"
        assert health.json()["components"]["worker"]["status"] == "unavailable"

        new_csrf = login.json()["csrfToken"]
        old_token = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": origin, "X-CSRF-Token": csrf_token},
        )
        assert old_token.status_code == 403
        cross_origin = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://untrusted.example", "X-CSRF-Token": new_csrf},
        )
        assert cross_origin.status_code == 403
        rejected = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": origin, "X-CSRF-Token": "wrong-token"},
        )
        assert rejected.status_code == 403
        current_session = await client.get("/api/v1/auth/session")
        assert current_session.status_code == 200
        assert current_session.headers["cache-control"] == "no-store"
        assert current_session.json()["csrfToken"] == new_csrf
        new_csrf = current_session.json()["csrfToken"]

        logout = await client.post(
            "/api/v1/auth/logout", headers={"Origin": origin, "X-CSRF-Token": new_csrf}
        )
        assert logout.status_code == 204
        assert (await client.get("/api/v1/auth/session")).status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected() -> None:
    store = MemoryAuthStore()
    expired_token = "expired-session-token"
    token_hash = hashlib.sha256(expired_token.encode()).hexdigest()
    store.sessions[token_hash] = AuthSession(
        token_hash=token_hash,
        owner_id=1,
        csrf_hash="unused",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    app = create_app(Settings(public_origin="http://localhost:3000", csrf_signing_secret="test-csrf-signing-secret"))

    async def session_override():
        yield MemorySession(store)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_auth_redis] = lambda: MemoryRedis()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:3000"
    ) as client:
        client.cookies.set("bbd_session", expired_token, domain="localhost", path="/")
        response = await client.get("/api/v1/auth/session")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limit_rejects_the_sixth_attempt() -> None:
    store = MemoryAuthStore()
    redis = MemoryRedis()
    app = create_app(Settings(public_origin="http://localhost:3000", csrf_signing_secret="test-csrf-signing-secret"))

    async def session_override():
        yield MemorySession(store)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_auth_redis] = lambda: redis
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:3000"
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        headers = {"Origin": "http://localhost:3000", "X-CSRF-Token": csrf}
        for _ in range(5):
            response = await client.post(
                "/api/v1/auth/login", headers=headers, json={"password": "wrong-password"}
            )
            assert response.status_code == 401
        limited = await client.post(
            "/api/v1/auth/login", headers=headers, json={"password": "wrong-password"}
        )
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
