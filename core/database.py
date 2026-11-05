import weakref
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import Request
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.metrics import register_engine

if TYPE_CHECKING:
    from core.config import Settings


class Base(DeclarativeBase):
    """Declarative SQLAlchemy metadata base shared by application persistence models."""


class CreatedAtMixin:
    """Reusable database-generated UTC creation timestamp column for persistent records."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Pooled engine -> its direct (unpooled) sibling; weak so a discarded engine never pins the sibling.
_DIRECT_ENGINES: "weakref.WeakKeyDictionary[AsyncEngine, AsyncEngine]" = weakref.WeakKeyDictionary()


def _pooled_connect_args() -> dict[str, Any]:
    """Return asyncpg connect args that are safe behind a transaction-pooling proxy such as PgBouncer.

    A server connection is shared between clients between transactions, so a prepared statement created by
    one client may be missing (or collide by name) on the next. Both asyncpg's own cache and SQLAlchemy's
    prepared-statement cache are therefore disabled, and any statement that is still prepared gets a
    globally unique name so two clients can never collide on ``__asyncpg_stmt_N__``.
    """
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4().hex}__",
    }


def build_connect_args(
    *, pgbouncer: bool, statement_timeout_ms: int = 0, idle_tx_timeout_ms: int = 0,
) -> dict[str, Any]:
    """Compute the asyncpg ``connect_args`` for one engine; pure so every mode is unit-testable.

    Direct mode sends the non-zero timeouts as startup ``server_settings`` (per-connection GUCs). Pooled mode
    sends none: a transaction pooler rejects or forgets startup parameters, so the pooler applies the role's
    timeouts itself (``connect_query`` per database alias, see ``docker-compose.pgbouncer.yml``) and the
    prepared-statement caches are disabled instead.
    """
    if pgbouncer:
        return _pooled_connect_args()
    settings = {
        name: str(value)
        for name, value in (
            ("statement_timeout", statement_timeout_ms),
            ("idle_in_transaction_session_timeout", idle_tx_timeout_ms),
        )
        if value
    }
    return {"server_settings": settings}


def make_session_factory(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    statement_timeout_ms: int = 0,
    idle_tx_timeout_ms: int = 0,
    pgbouncer: bool = False,
    direct_url: str | None = None,
    direct_pool_size: int = 4,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create a bounded-pool async engine and session factory.

    Direct mode: non-zero timeouts become per-connection server GUCs. PgBouncer mode (``pgbouncer=True``):
    prepared-statement caches are disabled, no startup GUCs are sent, and a second small engine on
    ``direct_url`` is attached for session-level features (see :func:`direct_engine`); ``direct_url`` is then
    required. No global lock_timeout is set: it would turn waits on the privacy advisory lock into errors.
    Both engines are registered for the Prometheus pool gauges (``core.metrics.register_engine``).

    Raises:
        ValueError: ``pgbouncer`` is true without ``direct_url``.
    """
    if pgbouncer and not direct_url:
        raise ValueError("PgBouncer mode requires a direct database URL for session-level features")
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=5,
        pool_recycle=1800,
        connect_args=build_connect_args(
            pgbouncer=pgbouncer, statement_timeout_ms=statement_timeout_ms, idle_tx_timeout_ms=idle_tx_timeout_ms,
        ),
    )
    register_engine(engine)  # pool size / checked-out / overflow gauges (read lazily at scrape time)
    if pgbouncer and direct_url:
        # Only session advisory leases use this engine; each holds one connection for a run/upload, so the
        # bound is the maximum number of concurrent leases, not request concurrency.
        _DIRECT_ENGINES[engine] = create_async_engine(
            direct_url,
            pool_pre_ping=True,
            pool_size=direct_pool_size,
            max_overflow=0,
            pool_timeout=5,
            pool_recycle=1800,
            connect_args=build_connect_args(pgbouncer=False, idle_tx_timeout_ms=idle_tx_timeout_ms),
        )
        register_engine(_DIRECT_ENGINES[engine])  # the session-lease pool gets its own gauge series
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def make_session_factory_for_role(
    settings: "Settings",
    *,
    pool_size: int,
    max_overflow: int,
    statement_timeout_ms: int,
    direct_pool_size: int | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build one process role's engine from validated settings, choosing direct or PgBouncer mode.

    Args:
        settings: Process settings; ``pgbouncer_enabled`` selects pooled mode and ``database_direct_url``
            feeds the session-lease engine.
        statement_timeout_ms: The role's ``statement_timeout`` (see ``Settings.db_*_statement_timeout_ms``).
        direct_pool_size: Concurrent session leases the role can hold; defaults to ``db_direct_pool_size``.
    """
    return make_session_factory(
        settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        statement_timeout_ms=statement_timeout_ms,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
        pgbouncer=settings.pgbouncer_enabled,
        direct_url=settings.database_direct_url,
        direct_pool_size=direct_pool_size or settings.db_direct_pool_size,
    )


def direct_engine(engine: AsyncEngine) -> AsyncEngine:
    """Return the engine for session-level PostgreSQL features (session advisory locks, LISTEN, WITH HOLD).

    In PgBouncer mode this is the direct (unpooled) engine attached by :func:`make_session_factory`; otherwise
    it is ``engine`` itself, because a direct connection already keeps one backend per checkout. Every
    ``pg_try_advisory_lock``/``pg_advisory_unlock`` site must obtain its connection here.
    """
    return _DIRECT_ENGINES.get(engine, engine)


async def dispose_engine(engine: AsyncEngine) -> None:
    """Dispose ``engine`` and its attached direct engine (if any); both are always attempted."""
    attached = _DIRECT_ENGINES.pop(engine, None)
    try:
        await engine.dispose()
    finally:
        if attached is not None:
            await attached.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped async database session and close it when dependency handling finishes."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session
