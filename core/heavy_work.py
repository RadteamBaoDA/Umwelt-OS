"""PostgreSQL capacity for one awaited local heavy job, independent of remote graph uncertainty."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import ParamSpec, TypeVar, cast
from uuid import UUID, uuid4

from arq import Retry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

P = ParamSpec("P")
R = TypeVar("R")
MAX_OPERATION_SECONDS = 150
HEAVY_LOCK_KEY = 732941801


class HeavyWorkBusy(TimeoutError):
    """Indicate cross-process contention before domain work is claimed."""


class HeavyLeaseLost(RuntimeError):
    """Reject continuing local work after its ownership connection fails."""


@dataclass(frozen=True)
class HeavyLease:
    """Identify local operation and monotonic deadline, never remote cessation.

    UUID is diagnostic identity; PostgreSQL transaction owns the advisory lock.
    """

    token: UUID
    deadline: float


@asynccontextmanager
async def heavy_job_slot(
    factory: async_sessionmaker[AsyncSession], *, timeout_seconds: float = 120,
) -> AsyncIterator[HeavyLease]:
    """Serialize awaited heavy work with a dedicated PostgreSQL transaction.

    Acquire before domain claims/source/document fences, retain through awaited
    coroutine cleanup/publication, then session rollback releases only this lock.
    Existing workers default to 120 seconds. Temporal may explicitly reserve
    150 seconds for its 120-second adapter deadline and 30-second owner overhead.
    Acquisition and work share that fixed deadline. Cancellation cleanup can
    exceed the deadline and retains capacity until complete. No detached host
    work is permitted: parsers must terminate/join children before returning.
    Connection death releases the server lock without a TTL takeover; it does
    not prove remote cancellation. Model slots/graph partition uncertainty stay
    under their respective owners. This helper never commits domain state.
    """
    if not 0 < timeout_seconds <= MAX_OPERATION_SECONDS:
        raise ValueError("Heavy operation timeout must be within 150 seconds")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    lease = HeavyLease(uuid4(), deadline)
    async with factory() as session:
        async with asyncio.timeout_at(deadline):
            async with asyncio.timeout(5):
                acquired = await session.scalar(text(
                    "SELECT pg_try_advisory_xact_lock(:key)"
                ), {"key": HEAVY_LOCK_KEY})
            if not acquired:
                raise HeavyWorkBusy("Another process owns heavy execution capacity")
            owner = asyncio.current_task()
            renewal_error: Exception | None = None

            async def renew() -> None:
                """Probe held connection periodically and cancel local work on loss.

                Transaction locks need no expiry extension. Probe failure cannot
                establish whether a dispatched remote operation has stopped.
                """
                nonlocal renewal_error
                try:
                    while True:
                        await asyncio.sleep(10)
                        async with asyncio.timeout(5):
                            await session.execute(text("SELECT 1"))
                except Exception as exc:
                    renewal_error = exc
                    if owner is not None:
                        owner.cancel()

            heartbeat = asyncio.create_task(renew())
            try:
                yield lease
                if renewal_error is not None:
                    raise HeavyLeaseLost("Heavy ownership connection failed") from renewal_error
            except asyncio.CancelledError:
                if renewal_error is not None:
                    raise HeavyLeaseLost("Heavy ownership connection failed") from renewal_error
                raise
            finally:
                # Consumer finally blocks finish before ownership session closes.
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass


def bounded_heavy_work(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap an ARQ consumer, preserving domain transaction/authorization fences.

    Contention retries before claims; cancellation remains visible to durable
    recovery and awaits consumer cleanup before releasing local capacity.
    """
    @wraps(function)
    async def bounded(*args: P.args, **kwargs: P.kwargs) -> R:
        """Acquire capacity before entering the worker and preserve its result."""
        ctx = cast(dict[str, object], args[0])
        factory = cast(async_sessionmaker[AsyncSession], ctx["session_factory"])
        try:
            async with heavy_job_slot(factory):
                return await function(*args, **kwargs)
        except HeavyWorkBusy as exc:
            raise Retry(defer=5) from exc
    return bounded
