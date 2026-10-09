import asyncio
import os
import time

import pytest
from httpx import AsyncClient

from tests.integration.conftest import post_after_throttle

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
        return await post_after_throttle(
            client,
            "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"password": password},
        )

    async with AsyncClient(base_url=base_url) as first, AsyncClient(base_url=base_url) as second:
        rotated_csrf: str | None = None
        try:
            csrf = (await login(first, old)).json()["csrfToken"]
            assert (await login(second, old)).status_code == 200
            changed = await first.post(
                "/api/v1/auth/password",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                json={"currentPassword": old, "newPassword": new},
            )
            assert changed.status_code == 200
            rotated_csrf = changed.json()["csrfToken"]
            assert (await first.get("/api/v1/auth/session")).status_code == 200
            assert rotated_csrf != csrf
            assert (await second.get("/api/v1/auth/session")).status_code == 401
            assert (await login(second, old)).status_code == 401
            assert (await login(second, new)).status_code == 200
        finally:
            if rotated_csrf is not None:
                # Restore the shared fixture password through the client holding the rotated session.
                restored = await post_after_throttle(
                    first,
                    "/api/v1/auth/password",
                    headers={"Origin": origin, "X-CSRF-Token": rotated_csrf},
                    json={"currentPassword": new, "newPassword": old},
                )
                assert restored.status_code == 200


@pytest.mark.asyncio
async def test_password_change_concurrent_with_login_leaves_consistent_state() -> None:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    old, new = "test-owner-password-42", "raced-owner-password-44"

    async def login(client: AsyncClient, password: str, *, throttled: bool = True):
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
        post = post_after_throttle if throttled else (lambda c, u, **kw: c.post(u, **kw))
        return await post(
            client,
            "/api/v1/auth/login",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"password": password},
        )

    async with AsyncClient(base_url=base_url) as owner, AsyncClient(base_url=base_url) as racer:
        rotated_csrf: str | None = None
        # The login throttle is a fixed per-minute window; start a fresh one so the unthrottled racer login really races.
        await asyncio.sleep(60 - time.time() % 60 + 0.5)
        try:
            csrf = (await login(owner, old)).json()["csrfToken"]
            change, raced = await asyncio.gather(
                owner.post(
                    "/api/v1/auth/password",
                    headers={"Origin": origin, "X-CSRF-Token": csrf},
                    json={"currentPassword": old, "newPassword": new},
                ),
                login(racer, old, throttled=False),
            )
            assert change.status_code in (200, 409)
            assert raced.status_code in (200, 401, 409)
            if change.status_code == 200:
                rotated_csrf = change.json()["csrfToken"]
                # Committed change: the rotated cookie works, the old password never does, raced sessions are revoked.
                assert (await owner.get("/api/v1/auth/session")).status_code == 200
                assert (await racer.get("/api/v1/auth/session")).status_code == 401
                assert (await login(racer, old)).status_code == 401
                assert (await login(racer, new)).status_code == 200
            else:
                # Lock contention rejected the change: the old password must still be intact.
                assert (await login(racer, old)).status_code == 200
        finally:
            if rotated_csrf is not None:
                restored = await post_after_throttle(
                    owner,
                    "/api/v1/auth/password",
                    headers={"Origin": origin, "X-CSRF-Token": rotated_csrf},
                    json={"currentPassword": new, "newPassword": old},
                )
                assert restored.status_code == 200
