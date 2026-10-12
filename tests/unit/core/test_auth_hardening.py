"""S2 auth hardening: client IP header, login backoff, HKDF keys, idle timeout, audit hashing."""
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from core.auth import routes
from core.auth.dependencies import _account_session, derive_key, session_middleware_secret
from core.auth.models import AuthSession


def _req(header: str = "", **headers: str) -> MagicMock:
    request = MagicMock()
    request.client.host = "172.18.0.5"
    request.headers = headers
    request.app.state.settings.auth_client_ip_header = header
    request.app.state.settings.auth_trust_forwarded_for = False
    return request


def test_client_ip_header_default_unset_uses_peer() -> None:
    assert routes._client_ip(_req(**{"cf-connecting-ip": "1.1.1.1"})) == "172.18.0.5"
    assert routes._client_ip(_req("cf-connecting-ip", **{"cf-connecting-ip": "1.1.1.1"})) == "1.1.1.1"
    assert routes._client_ip(_req("cf-connecting-ip")) == "172.18.0.5"  # missing header falls back


def test_hkdf_keys_are_independent_and_deterministic() -> None:
    assert derive_key("s", "csrf-hmac") == derive_key("s", "csrf-hmac")
    assert derive_key("s", "csrf-hmac") != derive_key("s", "session-cookie")
    assert session_middleware_secret("s") == session_middleware_secret("s")
    assert session_middleware_secret("") != session_middleware_secret("")


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, int] = {}
        self.locks: dict[str, int] = {}

    def pipeline(self, transaction=True):
        self._q = []
        return self
    def incr(self, key):
        self._q.append(key)
    def expire(self, *_a): return None
    async def execute(self):
        out = []
        for key in self._q:
            self.kv[key] = self.kv.get(key, 0) + 1
            out.append(self.kv[key])
        return out
    async def ttl(self, key): return self.locks.get(key, -2)
    async def set(self, key, _v, ex=None): self.locks[key] = ex
    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None); self.locks.pop(k, None)


@pytest.mark.asyncio
async def test_backoff_is_per_account_and_ip_and_resets_on_success() -> None:
    redis = FakeRedis()
    victim_ip, attacker_ip = _req(), _req()
    attacker_ip.client.host = "6.6.6.6"
    for _ in range(5):
        await routes._login_record(attacker_ip, redis, "owner@x.io", success=False)
    with pytest.raises(HTTPException) as exc:
        await routes._login_precheck(attacker_ip, redis, "owner@x.io")
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) >= 1
    await routes._login_precheck(victim_ip, redis, "owner@x.io")  # owner from own IP not locked out
    await routes._login_record(attacker_ip, redis, "owner@x.io", success=True)
    await routes._login_precheck(attacker_ip, redis, "owner@x.io")


@pytest.mark.asyncio
async def test_backoff_grows_exponentially_and_fails_closed() -> None:
    redis, request = FakeRedis(), _req()
    delays = []
    for _ in range(6):
        await routes._login_record(request, redis, None, success=False)
        delays.append(next(iter(redis.locks.values()), 0))
    assert delays[2:] == [1, 2, 4, 8][: len(delays) - 2]
    broken = MagicMock()
    broken.ttl = AsyncMock(side_effect=RedisError("down"))
    with pytest.raises(HTTPException) as exc:
        await routes._login_precheck(request, broken, None)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_login_global_cap_removed() -> None:
    request = _req()
    pipeline = MagicMock()
    pipeline.execute = AsyncMock(return_value=[1, True, 999, True])
    redis = MagicMock()
    redis.pipeline.return_value = pipeline
    await routes._allow_attempt(request, redis, "login")
    with pytest.raises(HTTPException):
        await routes._allow_attempt(request, redis, "reauthenticate")


def test_audit_hashes_ip_and_redacts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bbd.auth"):
        routes._audit(_req(), "login_failure", 1)
    text = caplog.text
    assert "auth.login_failure" in text and "172.18.0.5" not in text and '"account_id": 1' in text and "ip_hash" in text


class _Session:
    def __init__(self, row): self.row, self.commits = row, 0
    async def get(self, *_a): return self.row
    async def commit(self): self.commits += 1


@pytest.mark.asyncio
async def test_idle_session_rejected_and_active_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.auth.dependencies.get_active_account", AsyncMock(return_value=object()))
    now = datetime.now(UTC)
    request = MagicMock()
    request.app.state.settings.session_idle_hours = 8

    def row(seen): return AuthSession(token_hash="h", owner_id=1, csrf_hash="c", created_at=now - timedelta(hours=20),
                                      last_seen_at=seen, expires_at=now + timedelta(hours=4))
    with pytest.raises(HTTPException) as exc:
        await _account_session(request, _Session(row(now - timedelta(hours=9))), "tok")
    assert exc.value.status_code == 401
    session = _Session(row(now - timedelta(minutes=10)))
    await _account_session(request, session, "tok")
    assert session.commits == 1 and session.row.last_seen_at > now - timedelta(minutes=1)
