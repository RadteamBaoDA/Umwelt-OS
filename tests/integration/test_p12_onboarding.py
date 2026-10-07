"""P12 release acceptance: fresh-instance setup and resumable onboarding through the real API.

Order dependency: these tests ``TRUNCATE owner CASCADE`` (demo receipts, goals, automations go with
it) and so must not run before ``test_p12_demo_seed`` in the same database. The harness and default
pytest collection order (alphabetical) satisfy this.
"""

import os
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

OWNER_PASSWORD = "test-owner-password-42"
SETUP_TOKEN = "bbd-os-disposable-test-token"

pytestmark = pytest.mark.skipif(
    os.getenv("BBD_INTEGRATION") != "1", reason="requires disposable Compose test services"
)


async def _setup(
    client: AsyncClient, post: Callable[..., Awaitable[Response]], *, token: str | None,
    password: str = OWNER_PASSWORD,
) -> Response:
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
    headers = {"Origin": origin, "X-CSRF-Token": csrf}
    if token is not None:
        headers["X-Setup-Token"] = token
    return await post(client, "/api/v1/auth/setup", headers=headers, json={"password": password})


async def _reset_to_fresh_instance(engine: AsyncEngine) -> None:
    """Return the disposable database to its pre-setup state (the owner and every row that depends on it)."""
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE owner CASCADE"))


@pytest.fixture
async def fresh_instance(committed_engine: AsyncEngine) -> None:
    await _reset_to_fresh_instance(committed_engine)


async def test_fresh_setup_requires_token_creates_one_owner_and_rejects_a_second_setup(
    fresh_instance: None, committed_engine: AsyncEngine,
    post_throttled: Callable[..., Awaitable[Response]],
) -> None:
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    async with AsyncClient(base_url=base_url, timeout=30) as client:
        assert (await client.get("/api/v1/auth/setup-status")).json() == {"setupRequired": True}

        # Token gate: neither a missing nor a wrong token may create the owner.
        assert (await _setup(client, post_throttled, token=None)).status_code == 403
        assert (await _setup(client, post_throttled, token="not-the-setup-token")).status_code == 403
        assert (await client.get("/api/v1/auth/setup-status")).json() == {"setupRequired": True}
        async with committed_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM owner")) == 0

        created = await _setup(client, post_throttled, token=SETUP_TOKEN)
        assert created.status_code == 201 and created.json() == {"created": True}
        assert (await client.get("/api/v1/auth/setup-status")).json() == {"setupRequired": False}
        async with committed_engine.connect() as connection:
            rows = (await connection.execute(text("SELECT id, password_hash FROM owner"))).all()
        assert [row[0] for row in rows] == [1]
        assert OWNER_PASSWORD not in rows[0][1]  # only a password hash is stored

        # A second setup, even with the valid token and a different password, is rejected
        # and must not replace the owner credential.
        second = await _setup(client, post_throttled, token=SETUP_TOKEN, password="a-different-owner-password-99")
        assert second.status_code == 409
        async with committed_engine.connect() as connection:
            after = (await connection.execute(text("SELECT id, password_hash FROM owner"))).all()
        assert [tuple(row) for row in after] == [tuple(rows[0])]


async def test_onboarding_progress_is_ordered_revisioned_and_resumable(
    fresh_instance: None, ready_owner_client: AsyncClient,
    post_throttled: Callable[..., Awaitable[Response]],
) -> None:
    client = ready_owner_client
    state = (await client.get("/api/v1/settings/onboarding")).json()
    start_revision = state["configuration_revision"]
    assert state["current_step"] == "ai_privacy" and state["completed_at"] is None

    def update(revision: int, step: str, choice: str | None = None) -> dict[str, object]:
        return {"expected_revision": revision, "current_step": step, "data_choice": choice}

    # Steps cannot be skipped, and completion needs the earlier steps and an explicit data choice.
    skip = await client.put("/api/v1/settings/onboarding", json=update(start_revision, "sources"))
    assert skip.status_code == 409
    early = await client.put("/api/v1/settings/onboarding", json=update(start_revision, "complete"))
    assert early.status_code == 409

    revision = start_revision
    for step in ("capability", "sources"):
        saved = await client.put("/api/v1/settings/onboarding", json=update(revision, step))
        assert saved.status_code == 200 and saved.json()["current_step"] == step
        revision = saved.json()["configuration_revision"]
        assert revision > start_revision

    # A stale revision (a second tab) is rejected rather than overwriting progress.
    stale = await client.put("/api/v1/settings/onboarding", json=update(start_revision, "sample_or_import"))
    assert stale.status_code == 409

    choice = await client.put(
        "/api/v1/settings/onboarding", json=update(revision, "sample_or_import", "sample")
    )
    assert choice.status_code == 200
    revision = choice.json()["configuration_revision"]

    # Resume: a separate login (new session) reads the persisted server-side progress.
    base_url = os.getenv("BBD_API_URL", "http://localhost:38000")
    origin = os.getenv("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
    async with AsyncClient(base_url=base_url, timeout=30) as resumed:
        csrf = (await resumed.get("/api/v1/auth/csrf")).json()["csrfToken"]
        login = await post_throttled(
            resumed, "/api/v1/auth/login", headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"password": OWNER_PASSWORD},
        )
        login.raise_for_status()
        resumed.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrfToken"]})
        persisted = (await resumed.get("/api/v1/settings/onboarding")).json()
        assert persisted["current_step"] == "sample_or_import"
        assert persisted["data_choice"] == "sample"
        assert persisted["configuration_revision"] == revision

        indexing = await resumed.put("/api/v1/settings/onboarding", json=update(revision, "indexing"))
        assert indexing.status_code == 200
        done = await resumed.put(
            "/api/v1/settings/onboarding",
            json=update(indexing.json()["configuration_revision"], "complete"),
        )
        assert done.status_code == 200 and done.json()["completed_at"] is not None

        # Completed onboarding cannot be reopened.
        reopen = await resumed.put(
            "/api/v1/settings/onboarding",
            json=update(done.json()["configuration_revision"], "capability"),
        )
        assert reopen.status_code == 409
