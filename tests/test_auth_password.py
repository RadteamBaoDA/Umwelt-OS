from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import create_app
from core.auth.models import AuthSession
from core.auth.routes import _hash, get_auth_redis
from core.auth.service import verify_password
from core.config import Settings
from core.database import get_session
from tests.test_auth_session import MemoryAuthStore, MemoryRedis, MemorySession

ORIGIN = "http://localhost:3000"
OLD = "test-owner-password-42"
NEW = "brand-new-owner-password-7"


class DeletingSession(MemorySession):
    """Memory session that applies the revoke-all-owner-sessions DELETE to the store."""

    async def execute(self, statement, _params=None):
        if not str(statement).startswith("DELETE FROM auth_session"):
            return await super().execute(statement, _params)
        params = statement.compile().params
        for key in [k for k, v in self.store.sessions.items() if v.owner_id == params["owner_id_1"]]:
            del self.store.sessions[key]
        return type("R", (), {"rowcount": 1})()


async def _login(store: MemoryAuthStore):
    app = create_app(Settings(public_origin=ORIGIN, csrf_signing_secret="test-csrf-signing-secret"))

    async def session_override():
        yield DeletingSession(store)

    app.dependency_overrides[get_session] = session_override
    redis = MemoryRedis()
    app.dependency_overrides[get_auth_redis] = lambda: redis
    app.state.session_factory = lambda: DeletingSession(store)
    client = AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN)
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
    login = await client.post(
        "/api/v1/auth/login", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json={"password": OLD}
    )
    return client, login.json()["csrfToken"]


def _body(current: str = OLD, new: str = NEW) -> dict[str, str]:
    return {"currentPassword": current, "newPassword": new}


@pytest.mark.asyncio
async def test_change_password_rejections_leave_password_unchanged() -> None:
    store = MemoryAuthStore()
    client, csrf = await _login(store)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    assert (await client.post("/api/v1/auth/password", headers={"Origin": ORIGIN}, json=_body())).status_code == 403
    assert (await client.post("/api/v1/auth/password", headers={"X-CSRF-Token": csrf}, json=_body())).status_code == 403
    wrong = await client.post("/api/v1/auth/password", headers=headers, json=_body(current="nope-nope-nope"))
    assert wrong.status_code == 403
    assert "nope-nope-nope" not in wrong.text
    weak = await client.post("/api/v1/auth/password", headers=headers, json=_body(new="short"))
    assert weak.status_code == 422
    assert verify_password(store.owner.password_hash, OLD)


@pytest.mark.asyncio
async def test_change_password_rotates_session_and_swaps_credentials() -> None:
    store = MemoryAuthStore()
    client, csrf = await _login(store)
    old_cookie = client.cookies["bbd_session"]
    store.sessions["other"] = AuthSession(
        token_hash="other", owner_id=1, csrf_hash="x", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    response = await client.post(
        "/api/v1/auth/password", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json=_body()
    )
    assert response.status_code == 200
    assert response.json()["csrfToken"] != csrf
    new_cookie = response.cookies["bbd_session"]
    assert new_cookie != old_cookie
    assert len(store.sessions) == 1 and _hash(old_cookie) not in store.sessions
    assert _hash(new_cookie) in store.sessions
    assert (await client.get("/api/v1/auth/session")).status_code == 200
    assert not verify_password(store.owner.password_hash, OLD)
    assert verify_password(store.owner.password_hash, NEW)
    # Old cookie alone is dead; the new password logs in and the old one does not.
    stale = AsyncClient(transport=client._transport, base_url=ORIGIN, cookies={"bbd_session": old_cookie})
    assert (await stale.get("/api/v1/auth/session")).status_code == 401
    csrf2 = (await client.get("/api/v1/auth/csrf")).json()["csrfToken"]
    h = {"Origin": ORIGIN, "X-CSRF-Token": csrf2}
    assert (await client.post("/api/v1/auth/login", headers=h, json={"password": OLD})).status_code == 401
    assert (await client.post("/api/v1/auth/login", headers=h, json={"password": NEW})).status_code == 200


@pytest.mark.asyncio
async def test_change_password_rejects_unchanged_password() -> None:
    client, csrf = await _login(MemoryAuthStore())
    response = await client.post(
        "/api/v1/auth/password", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, json=_body(new=OLD)
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_change_password_is_rate_limited() -> None:
    client, csrf = await _login(MemoryAuthStore())
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    codes = [
        (await client.post("/api/v1/auth/password", headers=headers, json=_body(current="nope-nope-nope"))).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [403] * 5
    assert codes[5] == 429
