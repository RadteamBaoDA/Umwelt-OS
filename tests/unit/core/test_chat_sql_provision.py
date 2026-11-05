"""Unit tests for chat SQL reader SCRAM provisioning (no database)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from types import SimpleNamespace
from typing import Any

import pytest

from core import chat_sql_provision as mod

PW = "s3cret-Token_abc"


def test_verifier_validates_scram_client_proof() -> None:
    """A client proof derived independently from the password must verify against StoredKey/ServerKey."""
    v = mod.scram_sha256_verifier(PW, salt=b"0123456789abcdef")
    head, rest = v.split("$", 1)
    iters_salt, keys = rest.split("$")
    iters, salt_b64 = iters_salt.split(":")
    stored_b64, server_b64 = keys.split(":")
    assert head == "SCRAM-SHA-256" and int(iters) == 4096
    salted = hashlib.pbkdf2_hmac("sha256", PW.encode(), base64.b64decode(salt_b64), int(iters))
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    auth = b"n=,r=x,r=xy,s=z,i=4096,c=biws,r=xy"
    sig = hmac.new(hashlib.sha256(client_key).digest(), auth, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, sig, strict=True))
    recovered = bytes(a ^ b for a, b in zip(proof, sig, strict=True))
    assert hashlib.sha256(recovered).digest() == base64.b64decode(stored_b64)
    assert base64.b64decode(server_b64) == hmac.new(salted, b"Server Key", hashlib.sha256).digest()


@pytest.mark.parametrize("bad", ["", "has space", "café", "tab\t"])
def test_non_ascii_or_spaced_password_rejected(bad: str) -> None:
    """Passwords SASLprep could alter are refused rather than hashed differently from libpq."""
    with pytest.raises(ValueError):
        mod.scram_sha256_verifier(bad)


class _Conn:
    """Fake connection recording SQL and optionally failing the ALTER."""

    def __init__(self, log: list[str], fail: Exception | None, unsafe: bool = False) -> None:
        self.log, self.fail, self.unsafe = log, fail, unsafe

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def exec_driver_sql(self, sql: str) -> Any:
        self.log.append(sql)
        if self.fail and sql.startswith("ALTER"):
            raise self.fail
        return SimpleNamespace(
            first=lambda: (1,) if self.unsafe and sql.startswith("SELECT") else None
        )


class _Engine:
    """Fake engine yielding one fake connection."""

    def __init__(self, log: list[str], fail: Exception | None, unsafe: bool = False) -> None:
        self.conn = _Conn(log, fail, unsafe)

    def connect(self) -> _Conn:
        return self.conn

    async def dispose(self) -> None:
        return None


def _settings() -> Any:
    return SimpleNamespace(
        chat_sql_reader_database_url=f"postgresql+asyncpg://{mod.LOGIN_ROLE}:{PW}@h/db",
        admin_database_url="postgresql+asyncpg://a:b@h/db",
    )


def test_executed_sql_never_contains_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the SCRAM verifier is sent; engine hides parameters."""
    log: list[str] = []
    kwargs: dict[str, Any] = {}

    def fake(url: str, **kw: Any) -> _Engine:
        kwargs.update(kw)
        return _Engine(log, None)

    monkeypatch.setattr(mod, "create_async_engine", fake)
    assert asyncio.run(mod.provision(_settings())) == 0
    assert kwargs["hide_parameters"] is True
    assert any(s.startswith(f"ALTER ROLE {mod.LOGIN_ROLE} PASSWORD 'SCRAM-SHA-256$") for s in log)
    assert all(PW not in s for s in log)


def test_error_message_has_no_password_or_statement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failures print class + SQLSTATE only."""
    boom = RuntimeError(f"ALTER ROLE x PASSWORD 'SCRAM...' {PW}")
    monkeypatch.setattr(mod, "create_async_engine", lambda url, **kw: _Engine([], boom))
    assert asyncio.run(mod.provision(_settings())) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "SQLSTATE" in err
    assert PW not in err and "ALTER" not in err and "SCRAM" not in err


def test_distinct_runtime_user_only_when_usernames_differ() -> None:
    """A runtime user equal to the admin user needs no grant; a different one is returned for the group grant."""
    same = SimpleNamespace(
        database_url="postgresql+asyncpg://a:p@h/db",
        admin_database_url="postgresql+asyncpg://a:p@h/db",
    )
    diff = SimpleNamespace(
        database_url="postgresql+asyncpg://app:p@h/db",
        admin_database_url="postgresql+asyncpg://adm:p@h/db",
    )
    assert mod.distinct_runtime_user(same) is None  # type: ignore[arg-type]
    assert mod.distinct_runtime_user(diff) == "app"  # type: ignore[arg-type]
    assert asyncio.run(mod.grant_runtime_role(same)) == 0  # type: ignore[arg-type]


def _runtime_settings(user: str) -> Any:
    return SimpleNamespace(
        database_url=f"postgresql+asyncpg://{user}:p@h/db",
        admin_database_url="postgresql+asyncpg://adm:p@h/db",
    )


def test_grant_refuses_reader_login_as_runtime_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader login must never be granted the app group."""
    log: list[str] = []
    monkeypatch.setattr(mod, "create_async_engine", lambda url, **kw: _Engine(log, None))
    assert asyncio.run(mod.grant_runtime_role(_runtime_settings(mod.LOGIN_ROLE))) == 1
    assert log == []


def test_grant_refuses_unsafe_group_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A loginable/superuser umwelt_app blocks the grant with a generic message."""
    log: list[str] = []
    monkeypatch.setattr(
        mod, "create_async_engine", lambda url, **kw: _Engine(log, None, unsafe=True)
    )
    assert asyncio.run(mod.grant_runtime_role(_runtime_settings("app"))) == 1
    assert not any(s.startswith("GRANT") for s in log)
    assert "unsafe attributes" in capsys.readouterr().err


def test_grant_runs_for_safe_group_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """With safe attributes the grant is issued."""
    log: list[str] = []
    monkeypatch.setattr(mod, "create_async_engine", lambda url, **kw: _Engine(log, None))
    assert asyncio.run(mod.grant_runtime_role(_runtime_settings("app"))) == 0
    assert 'GRANT umwelt_app TO "app"' in log
