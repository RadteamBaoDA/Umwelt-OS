import os

import pytest
from httpx import AsyncClient

from core.auth.dependencies import require_owner, require_owner_write

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.mark.asyncio
async def test_public_auth_dependency_keeps_system_private(anonymous_client: AsyncClient) -> None:
    assert callable(require_owner) and callable(require_owner_write)
    response = await anonymous_client.get("/api/v1/system/health")
    assert response.status_code == 401
    invalid_session = await anonymous_client.get(
        "/api/v1/system/health", headers={"Cookie": "bbd_session=invalid-session-token"}
    )
    assert invalid_session.status_code == 401


@pytest.mark.asyncio
async def test_owner_client_can_read_system_health(owner_client: AsyncClient) -> None:
    response = await owner_client.get("/api/v1/system/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_owner_write_rejects_missing_origin_and_wrong_csrf(
    owner_client: AsyncClient,
) -> None:
    csrf_token = owner_client.headers["X-CSRF-Token"]
    owner_client.headers.pop("Origin")
    missing_origin = await owner_client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
    )
    assert missing_origin.status_code == 403

    owner_client.headers["Origin"] = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    wrong_csrf = await owner_client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong-session-token"}
    )
    assert wrong_csrf.status_code == 403
