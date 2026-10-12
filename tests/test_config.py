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


@pytest.fixture(autouse=True)
def _no_placeholder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop template-placeholder variables leaked into os.environ (e.g. from a developer .env) so mode tests are hermetic."""
    import os

    for key, value in list(os.environ.items()):
        if value.startswith(("replace-with-", "replace-this-")):
            monkeypatch.delenv(key)


def _prod(**kw: object) -> Settings:
    base = {
        "ENVIRONMENT": "production",
        "database_url": "postgresql+asyncpg://bbd:real-pw@db:5432/bbd",
        "public_origin": "https://example.com",
        "secure_cookies": True,
    }
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type]


def test_production_accepts_hardened_config() -> None:
    assert _prod().is_dev is False


@pytest.mark.parametrize(
    "override",
    [
        {"secure_cookies": False},
        {"setup_token": "replace-with-a-random-local-secret"},
        {"csrf_signing_secret": "replace-this-x"},
        {"database_url": "postgresql+asyncpg://bbd:bbd@postgres:5432/bbd"},
        {"database_url": "postgresql+asyncpg://bbd:replace-this-local-db-password@db:5432/bbd"},
    ],
)
def test_production_rejects_insecure_config(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _prod(**override)


def test_dev_allows_defaults_and_serves_docs() -> None:
    from apps.api.main import create_app

    dev = Settings(_env_file=None, ENVIRONMENT="development", setup_token="replace-with-x")
    assert dev.is_dev
    assert create_app(dev).docs_url == "/docs"
    assert create_app(_prod()).docs_url is None


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:3000", "http://127.0.0.1:3000", "http://192.168.1.20:3000", "http://10.0.0.5", "http://[::1]:3000", "http://nas.local:3000", "http://web:3000"],
)
def test_auto_mode_is_development_for_local_origin(origin: str) -> None:
    # Default "auto" + local/LAN origin relaxes transport only: http and non-Secure cookies pass with real secrets.
    settings = Settings(_env_file=None, ENVIRONMENT="auto", public_origin=origin,  # type: ignore[arg-type]
                        database_url="postgresql+asyncpg://bbd:real-pw@db:5432/bbd")
    assert settings.mode == "development" and settings.is_dev


def test_auto_mode_never_accepts_placeholder_secrets() -> None:
    # A real server with a mis-set local-looking origin must not start on template credentials.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ENVIRONMENT="auto", public_origin="http://localhost:3000", setup_token="replace-with-x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ENVIRONMENT="auto", public_origin="http://localhost:3000",  # type: ignore[arg-type]
                 database_url="postgresql+asyncpg://bbd:bbd@postgres:5432/bbd")


def test_auto_mode_is_production_for_public_domain() -> None:
    assert Settings(_env_file=None, ENVIRONMENT="auto", public_origin="https://example.com", secure_cookies=True,  # type: ignore[arg-type]
                    database_url="postgresql+asyncpg://bbd:real-pw@db:5432/bbd").mode == "production"
    with pytest.raises(ValidationError):  # http on a public domain
        Settings(_env_file=None, ENVIRONMENT="auto", public_origin="http://example.com", database_url="postgresql+asyncpg://bbd:real-pw@db:5432/bbd")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):  # https without Secure cookies
        Settings(_env_file=None, ENVIRONMENT="auto", public_origin="https://example.com", database_url="postgresql+asyncpg://bbd:real-pw@db:5432/bbd")  # type: ignore[arg-type]


def test_explicit_production_on_lan_keeps_secret_checks_but_allows_http() -> None:
    ok = {"ENVIRONMENT": "production", "public_origin": "http://192.168.1.20:3000", "database_url": "postgresql+asyncpg://bbd:real-pw@db:5432/bbd"}
    assert Settings(_env_file=None, **ok).mode == "production"  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**ok, "setup_token": "replace-with-x"})  # type: ignore[arg-type]


def test_explicit_development_wins_over_public_domain() -> None:
    assert Settings(_env_file=None, ENVIRONMENT="development", public_origin="https://example.com").mode == "development"  # type: ignore[arg-type]
    assert Settings(_env_file=None, ENVIRONMENT="bogus", public_origin="http://localhost:3000", database_url="postgresql+asyncpg://bbd:real-pw@db:5432/bbd").mode == "production"  # type: ignore[arg-type]
