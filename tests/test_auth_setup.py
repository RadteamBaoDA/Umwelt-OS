import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from apps.api.main import create_app
from core.auth.routes import get_auth_redis
from core.config import Settings
from core.database import get_session


class MemoryOwnerStore:
    owner_exists = False
    other_unique_conflict = False
    lock = asyncio.Lock()


class OwnerKeyConflict(Exception):
    sqlstate = "23505"
    constraint_name = "owner_pkey"


class OtherUniqueConflict(Exception):
    sqlstate = "23505"
    constraint_name = "owner_password_hash_key"


class MemorySession:
    def __init__(self, store: MemoryOwnerStore) -> None:
        self.store = store
        self.pending = None

    async def scalar(self, _statement):
        return 1 if self.store.owner_exists else None

    def add(self, value) -> None:
        self.pending = value

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        async with self.store.lock:
            if self.store.owner_exists or self.store.other_unique_conflict:
                conflict = OtherUniqueConflict if self.store.other_unique_conflict else OwnerKeyConflict
                raise IntegrityError("insert", {}, conflict())
            self.store.owner_exists = True

    async def rollback(self) -> None:
        return None


class MemoryRedis:
    def pipeline(self, transaction=True):
        return MemoryPipeline()


class MemoryPipeline:
    def __init__(self) -> None:
        self.increments = 0

    def incr(self, _key):
        self.increments += 1
        return self

    def expire(self, _key, _seconds, nx=True):
        return self

    async def execute(self):
        return [1, True] * self.increments


@pytest.mark.asyncio
async def test_only_one_simultaneous_setup_request_creates_the_owner() -> None:
    store = MemoryOwnerStore()
    settings = Settings(public_origin="http://localhost:3000", setup_token="test-setup-token", csrf_signing_secret="test-csrf-key")
    app = create_app(settings)

    async def session_override():
        yield MemorySession(store)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_auth_redis] = lambda: MemoryRedis()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:3000") as client:
        csrf_response = await client.get("/api/v1/auth/csrf")
        csrf_token = csrf_response.json()["csrfToken"]
        headers = {
            "X-Setup-Token": "test-setup-token",
            "X-CSRF-Token": csrf_token,
            "Origin": "http://localhost:3000",
        }
        async def setup():
            return await client.post(
                "/api/v1/auth/setup", headers=headers,
                json={"password": "test-owner-password-42"},
            )

        responses = await asyncio.gather(setup(), setup())

    assert sorted(response.status_code for response in responses) == [201, 409]


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_reported_as_owner_conflict() -> None:
    store = MemoryOwnerStore()
    store.other_unique_conflict = True
    settings = Settings(public_origin="http://localhost:3000", setup_token="test-setup-token", csrf_signing_secret="test-csrf-key")
    app = create_app(settings)

    async def session_override():
        yield MemorySession(store)

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_auth_redis] = lambda: MemoryRedis()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost:3000",
    ) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        response = await client.post(
            "/api/v1/auth/setup",
            headers={
                "X-Setup-Token": "test-setup-token",
                "X-CSRF-Token": csrf,
                "Origin": "http://localhost:3000",
            },
            json={"password": "test-owner-password-42"},
        )
    assert response.status_code == 500
    assert "owner_password_hash_key" not in response.text
