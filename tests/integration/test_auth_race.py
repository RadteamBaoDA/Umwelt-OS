import asyncio
import os

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


@pytest.mark.asyncio
async def test_simultaneous_setup_requests_create_exactly_one_owner() -> None:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")

    async with (
        AsyncClient(base_url=base_url) as first,
        AsyncClient(base_url=base_url) as second,
    ):
        first_csrf = (await first.get("/api/v1/auth/csrf")).json()["csrfToken"]
        second_csrf = (await second.get("/api/v1/auth/csrf")).json()["csrfToken"]

        async def create_owner(client: AsyncClient, csrf_token: str):
            return await client.post(
                "/api/v1/auth/setup",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf_token,
                    "X-Setup-Token": "bbd-os-disposable-test-token",
                },
                json={"password": "test-owner-password-42"},
            )

        responses = await asyncio.gather(
            create_owner(first, first_csrf), create_owner(second, second_csrf)
        )
        assert sorted(response.status_code for response in responses) == [201, 409]


@pytest.mark.asyncio
async def test_password_change_revokes_other_session_and_old_password() -> None:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    old, new = "test-owner-password-42", "rotated-owner-password-43"

    async def login(client: AsyncClient, password: str):
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        return await client.post(
            "/api/v1/auth/login", headers={"Origin": origin, "X-CSRF-Token": csrf}, json={"password": password}
        )

    async with AsyncClient(base_url=base_url) as first, AsyncClient(base_url=base_url) as second:
        csrf = (await login(first, old)).json()["csrfToken"]
        assert (await login(second, old)).status_code == 200
        changed = await first.post(
            "/api/v1/auth/password",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"currentPassword": old, "newPassword": new},
        )
        assert changed.status_code == 204
        assert (await first.get("/api/v1/auth/session")).status_code == 200
        assert changed.json()["csrfToken"] != csrf
        assert (await second.get("/api/v1/auth/session")).status_code == 401
        assert (await login(second, old)).status_code == 401
        relogin = await login(second, new)
        assert relogin.status_code == 200
        # Restore the shared fixture password for later tests.
        restored = await second.post(
            "/api/v1/auth/password",
            headers={"Origin": origin, "X-CSRF-Token": relogin.json()["csrfToken"]},
            json={"currentPassword": new, "newPassword": old},
        )
        assert restored.status_code == 204


@pytest.mark.asyncio
async def test_password_change_concurrent_with_login_leaves_consistent_state() -> None:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    old, new = "test-owner-password-42", "raced-owner-password-44"

    async def login(client: AsyncClient, password: str):
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        return await client.post(
            "/api/v1/auth/login", headers={"Origin": origin, "X-CSRF-Token": csrf}, json={"password": password}
        )

    async with AsyncClient(base_url=base_url) as owner, AsyncClient(base_url=base_url) as racer:
        csrf = (await login(owner, old)).json()["csrfToken"]
        change, raced = await asyncio.gather(
            owner.post(
                "/api/v1/auth/password",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                json={"currentPassword": old, "newPassword": new},
            ),
            login(racer, old),
        )
        assert change.status_code in (200, 409)
        assert raced.status_code in (200, 401, 409)
        if change.status_code == 200:
            # Committed change: the old password never works again and any raced session is revoked.
            assert (await racer.get("/api/v1/auth/session")).status_code == 401
            assert (await login(racer, old)).status_code == 401
            relogin = await login(racer, new)
            assert relogin.status_code == 200
            restored = await racer.post(
                "/api/v1/auth/password",
                headers={"Origin": origin, "X-CSRF-Token": relogin.json()["csrfToken"]},
                json={"currentPassword": new, "newPassword": old},
            )
            assert restored.status_code == 200
        else:
            # Lock contention rejected the change: the old password must still be intact.
            assert (await login(racer, old)).status_code == 200
