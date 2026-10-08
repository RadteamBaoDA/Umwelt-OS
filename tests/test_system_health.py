import asyncio
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.main import create_app
from core.config import Settings
from core.database import get_session
from core.system import health as health_module
from core.system import routes as system_routes
from core.system.health import system_health


@pytest.mark.asyncio
async def test_system_health_requires_an_owner_session() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings())), base_url="http://localhost:3000"
    ) as client:
        response = await client.get("/api/v1/system/health")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_system_health_times_out_stalled_dependency_probes(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "PROBE_TIMEOUT_SECONDS", 0.01, raising=False)

    class SlowSession:
        async def execute(self, _statement):
            await asyncio.sleep(0.05)

    class SlowRedis:
        async def ping(self):
            await asyncio.sleep(0.05)

        async def get(self, _key):
            return None

    result = await system_health(
        cast(AsyncSession, SlowSession()), cast(Redis, SlowRedis()), Settings()
    )
    assert result["components"]["postgres"]["status"] == "unavailable"
    assert result["components"]["redis"]["status"] == "unavailable"
    assert result["components"]["worker"]["status"] == "unavailable"
    assert result["components"]["chat_worker"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_readiness_times_out_a_stalled_database_probe(monkeypatch) -> None:
    monkeypatch.setattr(system_routes, "READY_TIMEOUT_SECONDS", 0.01)
    app = create_app(Settings())

    class SlowSession:
        async def execute(self, _statement):
            await asyncio.sleep(0.05)

    async def session_override():
        yield SlowSession()

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/system/ready")
    assert response.status_code == 503


def test_chat_worker_health_key_matches_worker_settings() -> None:
    from apps.worker.main import ChatWorkerSettings

    assert health_module.ARQ_CHAT_WORKER_HEALTH_KEY == ChatWorkerSettings.health_check_key


@pytest.mark.asyncio
async def test_missing_chat_worker_heartbeat_degrades_health() -> None:
    class FakeRedis:
        async def ping(self):
            return True

        async def get(self, key):
            return None if key == health_module.ARQ_CHAT_WORKER_HEALTH_KEY else b"1"

        async def info(self, _section):
            return {}

    class OkSession:
        async def execute(self, _statement):
            return None

    result = await system_health(cast(AsyncSession, OkSession()), cast(Redis, FakeRedis()), Settings())
    assert result["components"]["worker"]["status"] == "healthy"
    assert result["components"]["chat_worker"]["status"] == "unavailable"
    assert result["overall"] == "degraded"


@pytest.mark.asyncio
async def test_present_chat_worker_heartbeat_is_healthy() -> None:
    class FakeRedis:
        async def ping(self):
            return True

        async def get(self, _key):
            return b"1"

        async def info(self, _section):
            return {}

    class OkSession:
        async def execute(self, _statement):
            return None

    result = await system_health(cast(AsyncSession, OkSession()), cast(Redis, FakeRedis()), Settings())
    assert result["components"]["chat_worker"]["status"] == "healthy"
    assert result["overall"] == "healthy"


def test_ready_timeout_exceeds_health_probe_and_fits_compose_healthcheck() -> None:
    assert health_module.PROBE_TIMEOUT_SECONDS < system_routes.READY_TIMEOUT_SECONDS < 4
