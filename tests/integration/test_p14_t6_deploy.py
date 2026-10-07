"""P14-T6 acceptance: 2 API workers share the CSRF secret and the per-IP auth limit resists forged X-Forwarded-For.

The 45 s brief-through-web (:3300) check is deferred to T8: it needs the model request timeout set to >= 60 s.
"""

import os
import shutil

import pytest
from httpx import AsyncClient

from tests.integration.test_p14_chat_worker import _docker, _service_container

requires_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")
pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)

BASE_URL = os.getenv("BBD_API_URL", "http://localhost:38000")
ORIGIN = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")


@requires_docker
@pytest.mark.asyncio
async def test_api_runs_two_uvicorn_workers() -> None:
    container = _service_container("api")
    # `docker top` lists the uvicorn supervisor plus one multiprocessing child per worker.
    children = [line for line in _docker("top", container, "-o", "pid,args").splitlines() if "multiprocessing.spawn" in line]
    assert len(children) == 2, children


@pytest.mark.asyncio
async def test_login_then_20_fresh_connection_requests_succeed(ready_owner_client: AsyncClient) -> None:
    cookies = dict(ready_owner_client.cookies)
    csrf = ready_owner_client.headers["X-CSRF-Token"]
    for _ in range(20):
        # A new client per request opens a new connection, so the kernel spreads them across both workers.
        async with AsyncClient(base_url=BASE_URL, cookies=cookies, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}) as fresh:
            response = await fresh.get("/api/v1/auth/session")
            assert response.status_code == 200, response.text
            assert response.json()["authenticated"] is True


@pytest.mark.asyncio
async def test_forged_x_forwarded_for_cannot_exceed_per_ip_auth_limit() -> None:
    statuses: list[int] = []
    async with AsyncClient(base_url=BASE_URL, headers={"Origin": ORIGIN}) as client:
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        for index in range(8):
            response = await client.post(
                "/api/v1/auth/login",
                headers={"X-CSRF-Token": csrf, "X-Forwarded-For": f"203.0.113.{index + 1}"},
                json={"password": "definitely-wrong-password"},
            )
            statuses.append(response.status_code)
    # AUTH_TRUST_FORWARDED_FOR is off in the harness: every attempt shares the peer bucket, so the 6th is throttled.
    assert 429 in statuses[:6], statuses
