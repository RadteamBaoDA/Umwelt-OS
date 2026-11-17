"""Engine configuration per mode: direct (server GUC timeouts) versus PgBouncer transaction pooling."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from core import database
from core.config import Settings
from core.database import (
    build_connect_args,
    direct_engine,
    dispose_engine,
    make_session_factory,
    make_session_factory_for_role,
)

POOLED = "postgresql+asyncpg://u:p@pgbouncer:6432/bbd_api"
DIRECT = "postgresql+asyncpg://u:p@postgres:5432/bbd"


def test_direct_mode_sends_nonzero_timeouts_as_server_settings() -> None:
    """Direct connections carry per-connection GUCs and keep asyncpg's prepared-statement caches."""
    args = build_connect_args(
        pgbouncer=False, statement_timeout_ms=120_000, idle_tx_timeout_ms=240_000
    )
    assert args == {
        "server_settings": {
            "statement_timeout": "120000",
            "idle_in_transaction_session_timeout": "240000",
        }
    }
    assert build_connect_args(pgbouncer=False) == {"server_settings": {}}
    assert "statement_cache_size" not in args


def test_pgbouncer_mode_disables_prepared_statement_caches_and_startup_gucs() -> None:
    """Pooled connections must not cache prepared statements nor send startup parameters the pooler rejects."""
    args = build_connect_args(
        pgbouncer=True, statement_timeout_ms=60_000, idle_tx_timeout_ms=240_000
    )
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    assert "server_settings" not in args
    first, second = args["prepared_statement_name_func"](), args["prepared_statement_name_func"]()
    assert (
        first != second
    )  # unique per statement: two clients can never collide on a shared backend
    assert first.startswith("__asyncpg_")


def test_make_session_factory_requires_direct_url_in_pgbouncer_mode() -> None:
    """Falling back to the pooled URL for session locks would silently break them, so it is refused."""
    with pytest.raises(ValueError, match="direct database URL"):
        make_session_factory(POOLED, pgbouncer=True)


@pytest.mark.asyncio
async def test_pgbouncer_mode_attaches_a_bounded_direct_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pooled mode builds the pooled engine plus a small no-overflow direct engine for session leases."""
    captured: list[tuple[str, dict[str, Any]]] = []
    real = database.create_async_engine

    def spy(url: str, **kwargs: Any) -> AsyncEngine:
        """Record engine kwargs while still building a real (lazy, non-connecting) engine."""
        captured.append((url, kwargs))
        return real(url, **kwargs)

    monkeypatch.setattr(database, "create_async_engine", spy)
    engine, _factory = make_session_factory(
        POOLED,
        pool_size=7,
        pgbouncer=True,
        direct_url=DIRECT,
        direct_pool_size=3,
        idle_tx_timeout_ms=240_000,
    )
    try:
        (pooled_url, pooled), (direct_url, direct) = captured
        assert pooled_url == POOLED and pooled["connect_args"]["statement_cache_size"] == 0
        assert direct_url == DIRECT
        assert direct["pool_size"] == 3 and direct["max_overflow"] == 0
        assert direct["connect_args"] == {
            "server_settings": {"idle_in_transaction_session_timeout": "240000"}
        }
        assert direct_engine(engine) is not engine
        assert direct_engine(engine).url.render_as_string(hide_password=False) == DIRECT
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_direct_mode_has_no_second_engine() -> None:
    """Without PgBouncer ``direct_engine`` is the engine itself and ``dispose_engine`` is a plain dispose."""
    engine, _factory = make_session_factory(DIRECT, statement_timeout_ms=5_000)
    try:
        assert direct_engine(engine) is engine
    finally:
        await dispose_engine(engine)


def test_pgbouncer_setting_requires_direct_url() -> None:
    """``PGBOUNCER_ENABLED`` without ``DATABASE_DIRECT_URL`` fails at settings validation, before any connect."""
    with pytest.raises(ValidationError, match="DATABASE_DIRECT_URL"):
        Settings(_env_file=None, ENVIRONMENT="development", PGBOUNCER_ENABLED=True)  # type: ignore[call-arg]
    ok = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ENVIRONMENT="development",
        PGBOUNCER_ENABLED=True,
        DATABASE_DIRECT_URL=DIRECT,
    )
    assert ok.admin_database_url == DIRECT
    assert (
        Settings(_env_file=None, ENVIRONMENT="development").admin_database_url
        == Settings(  # type: ignore[call-arg]
            _env_file=None,
            ENVIRONMENT="development",
        ).database_url
    )


@pytest.mark.asyncio
async def test_role_factory_follows_settings_mode() -> None:
    """``make_session_factory_for_role`` selects pooled mode and the direct engine from settings."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ENVIRONMENT="development",
        PGBOUNCER_ENABLED=True,
        DATABASE_DIRECT_URL=DIRECT,
        DATABASE_URL=POOLED,
    )
    engine, _factory = make_session_factory_for_role(
        settings, pool_size=2, max_overflow=0, statement_timeout_ms=1
    )
    try:
        assert direct_engine(engine) is not engine
    finally:
        await dispose_engine(engine)


def test_per_role_statement_timeout_defaults_are_explicit() -> None:
    """Worker and model-worker no longer run unbounded; API/chat keep 60 s, workers get 120 s."""
    settings = Settings(_env_file=None, ENVIRONMENT="development")  # type: ignore[call-arg]
    assert settings.db_statement_timeout_ms == 60_000
    assert settings.db_worker_statement_timeout_ms == 120_000
    assert settings.db_model_worker_statement_timeout_ms == 120_000
    assert settings.db_idle_tx_timeout_ms == 240_000
