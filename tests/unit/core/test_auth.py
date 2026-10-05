"""Unit tests for core.auth modules.

Covers password hashing and verification, token hashing, CSRF signature generation
and verification, session authentication dependencies, origin checks,
public auth contracts, auth schemas, and rate limiters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from core.auth.dependencies import (
    CSRF_COOKIE,
    CSRF_MAX_AGE_SECONDS,
    SESSION_COOKIE,
    _csrf_signature,
    _current_session,
    _hash,
    _origin_allowed,
    _valid_csrf,
    require_owner,
    require_owner_write,
)
from core.auth.models import AuthSession, Owner
from core.auth.public import get_demo_owner_id, revalidate_owner_session
from core.auth.routes import _allow_attempt, _is_owner_conflict, _new_csrf
from core.auth.schemas import (
    AuthState,
    CsrfResponse,
    LoginRequest,
    SetupRequest,
    SetupResponse,
    SetupStatus,
)
from core.auth.service import hash_password, verify_password
from core.config import Settings


class TestPasswordHashing:
    """Unit tests for Argon2 password hashing and verification."""

    def test_hash_password_produces_argon2_hash(self) -> None:
        """Verify hash_password returns an Argon2 format hash."""
        password = "CorrectHorseBatteryStaple123!"
        hashed = hash_password(password)
        assert hashed.startswith("$argon2id$")
        assert hashed != password

    def test_verify_password_matches_correct_password(self) -> None:
        """Verify verify_password returns True for matching password."""
        password = "MySecurePassword456!"
        hashed = hash_password(password)
        assert verify_password(hashed, password) is True

    def test_verify_password_rejects_wrong_password(self) -> None:
        """Verify verify_password returns False for incorrect password."""
        password = "CorrectPassword123!"
        hashed = hash_password(password)
        assert verify_password(hashed, "WrongPassword123!") is False

    def test_verify_password_raises_on_invalid_hash_structure(self) -> None:
        """Verify verify_password raises InvalidHashError on structurally invalid hash strings."""
        from argon2.exceptions import InvalidHashError

        with pytest.raises(InvalidHashError):
            verify_password("not-a-valid-argon2-hash", "password")


class TestTokenAndCsrfHelpers:
    """Unit tests for hashing, CSRF signature generation, and CSRF token validation."""

    def test_hash_helper(self) -> None:
        """Verify _hash returns SHA-256 digest of input."""
        value = "sample-token-123"
        expected = hashlib.sha256(value.encode()).hexdigest()
        assert _hash(value) == expected

    def test_csrf_signature_generation(self) -> None:
        """Verify _csrf_signature signs token and expiry with secret."""
        settings = Settings(csrf_signing_secret=SecretStr("csrf-test-secret"))
        token = "token123"
        expiry = int(time.time()) + 300
        sig = _csrf_signature(token, expiry, settings)
        assert len(sig) == 64

    def test_csrf_signature_fails_closed_when_secret_empty(self) -> None:
        """Verify _csrf_signature raises 503 when signing secret is empty."""
        settings = Settings(csrf_signing_secret=SecretStr(""))
        with pytest.raises(HTTPException) as exc_info:
            _csrf_signature("token", 12345, settings)
        assert exc_info.value.status_code == 503

    def test_valid_csrf_success(self) -> None:
        """Verify _valid_csrf returns True for valid matching cookie and header."""
        settings = Settings(csrf_signing_secret=SecretStr("csrf-secret"))
        token, cookie_val = _new_csrf(settings)
        assert _valid_csrf(cookie_val, token, settings) is True

    def test_valid_csrf_rejects_tampered_signature(self) -> None:
        """Verify _valid_csrf rejects altered signature."""
        settings = Settings(csrf_signing_secret=SecretStr("csrf-secret"))
        token, cookie_val = _new_csrf(settings)
        parts = cookie_val.split(".")
        tampered_cookie = f"{parts[0]}.{parts[1]}.{'0' * 64}"
        assert _valid_csrf(tampered_cookie, token, settings) is False

    def test_valid_csrf_rejects_expired_cookie(self) -> None:
        """Verify _valid_csrf rejects expired timestamp."""
        settings = Settings(csrf_signing_secret=SecretStr("csrf-secret"))
        token = "token123"
        past_expiry = int(time.time()) - 10
        sig = _csrf_signature(token, past_expiry, settings)
        cookie = f"{token}.{past_expiry}.{sig}"
        assert _valid_csrf(cookie, token, settings) is False

    def test_valid_csrf_rejects_mismatched_token(self) -> None:
        """Verify _valid_csrf rejects submitted header token not matching cookie token."""
        settings = Settings(csrf_signing_secret=SecretStr("csrf-secret"))
        token, cookie_val = _new_csrf(settings)
        assert _valid_csrf(cookie_val, "different-token", settings) is False


class TestOriginValidation:
    """Unit tests for _origin_allowed origin checking."""

    def test_origin_allowed_matching(self) -> None:
        """Verify origin matching configured public_origin is allowed."""
        settings = Settings(public_origin="https://app.example.com")
        assert _origin_allowed("https://app.example.com", settings) is True
        assert _origin_allowed("https://app.example.com/", settings) is True

    def test_origin_allowed_rejects_mismatch(self) -> None:
        """Verify differing origin is rejected."""
        settings = Settings(public_origin="https://app.example.com")
        assert _origin_allowed("https://evil.com", settings) is False
        assert _origin_allowed(None, settings) is False


class TestSessionDependencies:
    """Unit tests for _current_session, require_owner, and require_owner_write."""

    @pytest.mark.asyncio
    async def test_current_session_missing_token_raises_401(self) -> None:
        """Verify _current_session raises 401 when token is missing."""
        request = MagicMock()
        session = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await _current_session(request, session, None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_current_session_not_found_raises_401(self) -> None:
        """Verify _current_session raises 401 when token is not in DB."""
        request = MagicMock()
        session = AsyncMock()
        session.get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            await _current_session(request, session, "unknown-token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_current_session_expired_raises_401(self) -> None:
        """Verify _current_session raises 401 when session has expired."""
        request = MagicMock()
        session = AsyncMock()
        expired_row = AuthSession(
            token_hash=_hash("expired-token"),
            owner_id=1,
            csrf_hash="csrf",
            created_at=datetime.now(UTC) - timedelta(hours=2),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.get.return_value = expired_row
        with pytest.raises(HTTPException) as exc_info:
            await _current_session(request, session, "expired-token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_current_session_valid_success(self) -> None:
        """Verify _current_session returns row and sets request.state.auth_session."""
        request = MagicMock()
        request.state = MagicMock()
        session = AsyncMock()
        valid_row = AuthSession(
            token_hash=_hash("valid-token"),
            owner_id=1,
            csrf_hash="csrf",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.get.return_value = valid_row
        result = await _current_session(request, session, "valid-token")
        assert result == valid_row
        assert request.state.auth_session == valid_row

    @pytest.mark.asyncio
    async def test_require_owner_reads_cookie(self) -> None:
        """Verify require_owner extracts SESSION_COOKIE from request."""
        request = MagicMock()
        request.cookies = {SESSION_COOKIE: "cookie-token"}
        session = AsyncMock()
        valid_row = AuthSession(
            token_hash=_hash("cookie-token"),
            owner_id=1,
            csrf_hash="csrf",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.get.return_value = valid_row
        res = await require_owner(request, session)
        assert res == valid_row

    @pytest.mark.asyncio
    async def test_require_owner_write_origin_disallowed_raises_403(self) -> None:
        """Verify require_owner_write rejects disallowed origin before auth lookup."""
        request = MagicMock()
        settings = Settings(public_origin="https://app.example.com")
        request.app.state.settings = settings
        session = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await require_owner_write(
                request=request,
                session=session,
                origin="https://attacker.com",
                csrf_token="any",
            )
        assert exc_info.value.status_code == 403


class TestPublicAuthContracts:
    """Unit tests for public functions revalidate_owner_session and get_demo_owner_id."""

    @pytest.mark.asyncio
    async def test_revalidate_owner_session_found(self) -> None:
        """Verify revalidate_owner_session returns True when row is found."""
        session = AsyncMock()
        session.scalar.return_value = "token_hash_abc"
        res = await revalidate_owner_session(session, "token_hash_abc", owner_id=1)
        assert res is True

    @pytest.mark.asyncio
    async def test_revalidate_owner_session_not_found(self) -> None:
        """Verify revalidate_owner_session returns False when row is None."""
        session = AsyncMock()
        session.scalar.return_value = None
        res = await revalidate_owner_session(session, "missing_hash", owner_id=1)
        assert res is False

    @pytest.mark.asyncio
    async def test_get_demo_owner_id_success(self) -> None:
        """Verify get_demo_owner_id returns 1 when owner exists."""
        session = AsyncMock()
        session.scalar.return_value = 1
        owner_id = await get_demo_owner_id(session)
        assert owner_id == 1

    @pytest.mark.asyncio
    async def test_get_demo_owner_id_missing_raises_runtime_error(self) -> None:
        """Verify get_demo_owner_id raises RuntimeError when owner does not exist."""
        session = AsyncMock()
        session.scalar.return_value = None
        with pytest.raises(RuntimeError, match="Demo seeding requires"):
            await get_demo_owner_id(session)


class TestAuthSchemas:
    """Unit tests for Pydantic models in core.auth.schemas."""

    def test_setup_request_validates_password_length(self) -> None:
        """Verify SetupRequest requires password between 12 and 128 characters."""
        assert SetupRequest(password="password12345").password == "password12345"
        with pytest.raises(ValidationError):
            SetupRequest(password="short")

    def test_login_request_validates_password(self) -> None:
        """Verify LoginRequest accepts valid non-empty password up to 128 chars."""
        assert LoginRequest(password="pass").password == "pass"
        with pytest.raises(ValidationError):
            LoginRequest(password="")

    def test_auth_state_defaults(self) -> None:
        """Verify AuthState default fields."""
        state = AuthState(csrfToken="tok")
        assert state.authenticated is True
        assert state.csrfToken == "tok"


class TestAuthRateLimiter:
    """Unit tests for _allow_attempt rate limiter logic with Redis."""

    @pytest.mark.asyncio
    async def test_allow_attempt_success(self) -> None:
        """Verify _allow_attempt allows attempt within thresholds."""
        request = MagicMock()
        request.client.host = "127.0.0.1"
        redis = MagicMock()
        pipeline = MagicMock()
        pipeline.execute = AsyncMock(return_value=[1, True, 1, True])
        redis.pipeline.return_value = pipeline

        await _allow_attempt(request, redis, "login")

    @pytest.mark.asyncio
    async def test_allow_attempt_rate_limited_per_ip(self) -> None:
        """Verify _allow_attempt raises 429 when per-ip count > 5."""
        request = MagicMock()
        request.client.host = "127.0.0.1"
        redis = MagicMock()
        pipeline = MagicMock()
        pipeline.execute = AsyncMock(return_value=[6, True, 10, True])
        redis.pipeline.return_value = pipeline

        with pytest.raises(HTTPException) as exc_info:
            await _allow_attempt(request, redis, "login")
        assert exc_info.value.status_code == 429
        assert "Retry-After" in exc_info.value.headers

    @pytest.mark.asyncio
    async def test_allow_attempt_redis_error_raises_503(self) -> None:
        """Verify Redis failure in _allow_attempt raises 503."""
        request = MagicMock()
        request.client.host = "127.0.0.1"
        redis = MagicMock()
        pipeline = MagicMock()
        pipeline.execute = AsyncMock(side_effect=RedisError("Redis down"))
        redis.pipeline.return_value = pipeline

        with pytest.raises(HTTPException) as exc_info:
            await _allow_attempt(request, redis, "login")
        assert exc_info.value.status_code == 503
