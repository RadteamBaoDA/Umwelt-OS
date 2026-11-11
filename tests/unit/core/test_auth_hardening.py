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
    assert (
        routes._client_ip(_req("cf-connecting-ip", **{"cf-connecting-ip": "1.1.1.1"})) == "1.1.1.1"
    )
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

    def expire(self, *_a):
        return None

    async def execute(self):
        out = []
        for key in self._q:
            self.kv[key] = self.kv.get(key, 0) + 1
            out.append(self.kv[key])
        return out

    async def ttl(self, key):
        return self.locks.get(key, -2)

    async def set(self, key, _v, ex=None):
        self.locks[key] = ex

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.locks.pop(k, None)


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
    with caplog.at_level(logging.INFO, logger="umwelt.auth"):
        routes._audit(_req(), "login_failure", 1)
    text = caplog.text
    assert (
        "auth.login_failure" in text
        and "172.18.0.5" not in text
        and '"account_id": 1' in text
        and "ip_hash" in text
    )


class _Session:
    def __init__(self, row):
        self.row, self.commits = row, 0

    async def get(self, *_a):
        return self.row

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_idle_session_rejected_and_active_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.auth.dependencies.get_active_account", AsyncMock(return_value=object())
    )
    now = datetime.now(UTC)
    request = MagicMock()
    request.app.state.settings.session_idle_hours = 8

    def row(seen):
        return AuthSession(
            token_hash="h",
            owner_id=1,
            csrf_hash="c",
            created_at=now - timedelta(hours=20),
            last_seen_at=seen,
            expires_at=now + timedelta(hours=4),
        )

    with pytest.raises(HTTPException) as exc:
        await _account_session(request, _Session(row(now - timedelta(hours=9))), "tok")
    assert exc.value.status_code == 401
    session = _Session(row(now - timedelta(minutes=10)))
    await _account_session(request, session, "tok")
    assert session.commits == 1 and session.row.last_seen_at > now - timedelta(minutes=1)


def test_client_ip_forwarded_for_uses_rightmost_entry_only_when_trusted() -> None:
    request = _req(**{"x-forwarded-for": "6.6.6.6, 203.0.113.9"})
    assert routes._client_ip(request) == "172.18.0.5"  # untrusted: spoofable header ignored
    request.app.state.settings.auth_trust_forwarded_for = True
    assert routes._client_ip(request) == "203.0.113.9"
    # A configured header wins and also takes its last comma entry.
    assert routes._client_ip(_req("x-real-ip", **{"x-real-ip": "9.9.9.9, 8.8.8.8"})) == "8.8.8.8"


def _login_request(csrf_ok: bool) -> tuple[MagicMock, MagicMock]:
    request = _req()
    request.cookies = {}
    request.app.state.settings.public_origin = "https://example.com"
    redis = MagicMock()
    redis.pipeline.side_effect = AssertionError("rate limiter must not run before CSRF validation")
    redis.ttl = AsyncMock(side_effect=AssertionError("lockout must not run before CSRF validation"))
    return request, redis


@pytest.mark.asyncio
async def test_login_does_not_count_attempt_before_csrf_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2.5: a request failing CSRF must not consume the per-IP budget or touch lockout state."""
    from core.auth.routes import LoginRequest

    monkeypatch.setattr(routes, "_origin_allowed", lambda *_: True)
    monkeypatch.setattr(routes, "_valid_csrf", lambda *_: False)
    request, redis = _login_request(csrf_ok=False)
    with pytest.raises(HTTPException) as caught:
        await routes.login(
            request,
            MagicMock(),
            LoginRequest(identifier="a@example.com", password="x" * 12),
            MagicMock(),
            redis,
            origin="https://example.com",
            csrf_token="bad",
        )
    assert caught.value.status_code == 403
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_setup_does_not_count_attempt_before_csrf_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2.5: owner setup with a bad CSRF token returns 403 without touching the rate limiter."""
    from core.auth.schemas import SetupRequest

    monkeypatch.setattr(routes, "_origin_allowed", lambda *_: True)
    monkeypatch.setattr(routes, "_valid_csrf", lambda *_: False)
    request, redis = _login_request(csrf_ok=False)
    request.app.state.settings.setup_token.get_secret_value.return_value = "tok"
    with pytest.raises(HTTPException) as caught:
        await routes.create_owner(
            SetupRequest.model_construct(
                email="a@example.com", password="x" * 12, display_name="A"
            ),
            request,
            MagicMock(),
            MagicMock(),
            redis,
            x_setup_token="tok",
            origin="https://example.com",
            csrf_token="bad",
        )
    assert caught.value.status_code == 403
    redis.pipeline.assert_not_called()
