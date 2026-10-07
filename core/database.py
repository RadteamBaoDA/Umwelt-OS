from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import Request
from sqlalchemy import DateTime, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative SQLAlchemy metadata base shared by application persistence models."""


class CreatedAtMixin:
    """Reusable database-generated UTC creation timestamp column for persistent records."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def make_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """Create an async SQLAlchemy engine with bounded pooling and a session factory that keeps loaded attributes usable after commit."""
    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=0)
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped async database session and close it when dependency handling finishes."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session
