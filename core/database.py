from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import Request
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative SQLAlchemy metadata base shared by application persistence models."""


class CreatedAtMixin:
    """Reusable database-generated UTC creation timestamp column for persistent records."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def make_session_factory(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    statement_timeout_ms: int = 0,
    idle_tx_timeout_ms: int = 0,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create a bounded-pool async engine and session factory; non-zero timeouts become per-connection server GUCs.

    No global lock_timeout is set: it would turn waits on the privacy advisory lock into errors.
    """
    settings = {
        name: str(value)
        for name, value in (
            ("statement_timeout", statement_timeout_ms),
            ("idle_in_transaction_session_timeout", idle_tx_timeout_ms),
        )
        if value
    }
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=5,
        pool_recycle=1800,
        connect_args={"server_settings": settings},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped async database session and close it when dependency handling finishes."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session
