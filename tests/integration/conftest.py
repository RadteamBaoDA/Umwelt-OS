import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


@pytest.fixture
async def anonymous_client() -> AsyncIterator[AsyncClient]:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    async with AsyncClient(base_url=base_url, headers={"Origin": origin}) as client:
        yield client


@pytest.fixture
async def owner_client() -> AsyncIterator[AsyncClient]:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    async with AsyncClient(base_url=base_url) as client:
        csrf_response = await client.get("/api/v1/auth/csrf")
        csrf_response.raise_for_status()
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf_response.json()["csrfToken"]},
            json={"password": "test-owner-password-42"},
        )
        login.raise_for_status()
        client.headers.update(
            {"Origin": origin, "X-CSRF-Token": login.json()["csrfToken"]}
        )
        yield client


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is available only in the disposable test runner")
    parsed = make_url(database_url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.database != "bbd_test"
        or parsed.username != "bbd_test"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail("Refusing database fixture URL outside the disposable local bbd_test database")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    async with engine.connect() as connection:
        database, user = (await connection.execute(text("SELECT current_database(), current_user"))).one()
        if database != "bbd_test" or user != "bbd_test":
            pytest.fail("Refusing database fixture connection outside bbd_test/bbd_test")
        await connection.rollback()
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


OWNER_PASSWORD = "test-owner-password-42"
SETUP_TOKEN = "bbd-os-disposable-test-token"


def disposable_database_url() -> str:
    """Return the disposable bbd_test URL or refuse; P12 acceptance tests never touch other databases."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is available only in the disposable test runner")
    parsed = make_url(database_url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.database != "bbd_test"
        or parsed.username != "bbd_test"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        pytest.fail("Refusing database URL outside the disposable local bbd_test database")
    return database_url


@pytest.fixture
async def committed_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an engine on the disposable bbd_test database whose commits are real and visible to the stack."""
    engine = create_async_engine(disposable_database_url(), pool_size=2, max_overflow=2)
    async with engine.connect() as connection:
        identity = (await connection.execute(text("SELECT current_database(), current_user"))).one()
        if tuple(identity) != ("bbd_test", "bbd_test"):
            pytest.fail("Refusing P12 acceptance connection outside bbd_test/bbd_test")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def scratch_database(
    committed_engine: AsyncEngine,
) -> AsyncIterator[Callable[[], Awaitable[str]]]:
    """Create throwaway databases on the disposable PostgreSQL server and drop them afterwards."""
    created: list[str] = []
    admin = committed_engine.execution_options(isolation_level="AUTOCOMMIT")

    async def create() -> str:
        name = f"bbd_p12_{uuid4().hex[:12]}"
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return str(make_url(disposable_database_url()).set(database=name).render_as_string(False))

    try:
        yield create
    finally:
        async with admin.connect() as connection:
            for name in created:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def post_after_throttle(client: AsyncClient, url: str, **kwargs: Any) -> Response:
    """POST, waiting out the per-minute authentication throttle (429) instead of failing on it."""
    for _ in range(4):
        response = await client.post(url, **kwargs)
        if response.status_code != 429:
            return response
        await asyncio.sleep(int(response.headers.get("Retry-After", "5")) + 1)
    return response


@pytest.fixture
def post_throttled() -> Callable[..., Awaitable[Response]]:
    return post_after_throttle


@pytest.fixture
async def ready_owner_client() -> AsyncIterator[AsyncClient]:
    """Return a logged-in owner client, running the real first-run setup first when no owner exists."""
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    async with AsyncClient(base_url=base_url, timeout=30) as client:
        status = (await client.get("/api/v1/auth/setup-status")).json()
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        if status["setupRequired"]:
            created = await post_after_throttle(
                client,
                "/api/v1/auth/setup",
                headers={"Origin": origin, "X-CSRF-Token": csrf, "X-Setup-Token": SETUP_TOKEN},
                json={"password": OWNER_PASSWORD},
            )
            assert created.status_code == 201
            csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        login = await post_after_throttle(
            client, "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"password": OWNER_PASSWORD},
        )
        login.raise_for_status()
        client.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrfToken"]})
        yield client
