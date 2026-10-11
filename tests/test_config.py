import pytest
from pydantic import ValidationError

from core.config import Settings


def test_invalid_public_origin_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(public_origin="not-a-url")


def test_n8n_key_is_redacted() -> None:
    settings = Settings(n8n_api_key="test-secret-only")
    assert "test-secret-only" not in repr(settings)


def test_credential_bearing_urls_are_redacted() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://bbd:db-secret@db:5432/bbd",
        redis_url="redis://:redis-secret@redis:6379/0",
    )
    rendered = repr(settings)
    assert all(secret not in rendered for secret in ("db-secret", "redis-secret"))


@pytest.mark.asyncio
async def test_liveness_endpoint_does_not_require_services() -> None:
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app(Settings())), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
