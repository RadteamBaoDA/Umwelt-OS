"""Dead-letter record for arq jobs that exhausted ``max_tries``, plus an owner-run replay CLI.

arq drops a job silently once its tries are exhausted (the failure only lives in a Redis result for an
hour). ``dead_letter`` wraps a job coroutine so that whenever arq will not run the job again (an ordinary
exception on any try, a timeout, or a ``Retry``/cancellation on the final try) one ``worker_dead_letters``
row is persisted before the exception propagates unchanged.

Privacy: the row never stores job arguments. It keeps a SHA-256 digest of the canonical arguments, and
only when every positional argument is an identifier-shaped scalar (UUID/slug, no ``@``/spaces) and there
are no keyword arguments does it keep those ids in ``args_ref`` and mark the row ``replayable``. Error
class names are stored as-is; messages are scrubbed (emails, URLs, long tokens) and truncated.

CLI (run from the api image, owner/operator only; replay is a dry run unless ``--execute`` is given)::

    python -m core.worker_dlq list [--limit N]
    python -m core.worker_dlq replay [--id UUID ...] [--execute]
    python -m core.worker_dlq purge --older-than-days N [--execute]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any
from uuid import UUID

from arq.worker import Retry, RetryJob
from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base
from core.ids import uuid7

_log = logging.getLogger(__name__)

ERROR_MESSAGE_LIMIT = 300
_ID_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.\-]{0,127}$")
_SCRUBBERS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "<token>"),
)


class WorkerDeadLetter(Base):
    """One exhausted job: identity, argument digest, scrubbed error and attempt/failure bookkeeping.

    ``(queue, job_id)`` is unique; a replayed job that fails again updates ``attempts``/``last_failed_at``
    and clears ``replayed_at`` instead of adding a row. Holds no workspace or user data.
    """

    __tablename__ = "worker_dead_letters"
    __table_args__ = (
        UniqueConstraint("queue", "job_id", name="uq_worker_dead_letters_queue_job"),
        Index("ix_worker_dead_letters_last_failed", "last_failed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    queue: Mapped[str] = mapped_column(String(64), nullable=False)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    job_id: Mapped[str] = mapped_column(String(256), nullable=False)
    args_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    args_ref: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    replayable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    error_class: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str] = mapped_column(String(ERROR_MESSAGE_LIMIT), nullable=False, server_default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


def args_digest(args: Sequence[Any], kwargs: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the arguments (``repr`` fallback); the arguments themselves are not kept."""
    canonical = json.dumps([list(args), kwargs], sort_keys=True, default=repr, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def replayable_refs(args: Sequence[Any], kwargs: dict[str, Any]) -> list[str] | None:
    """Return the positional ids when the call is safe to store and replay, else ``None``.

    Safe means: no kwargs and every positional argument is an identifier-shaped string (UUIDs, slugs).
    Anything else (free text, payloads, emails) is never persisted.
    """
    if kwargs or not all(isinstance(a, str) and _ID_LIKE.match(a) for a in args):
        return None
    return [str(a) for a in args]


def scrub_message(message: str) -> str:
    """Redact emails, URLs and long token-like runs from an exception message and truncate it."""
    for pattern, replacement in _SCRUBBERS:
        message = pattern.sub(replacement, message)
    return message[:ERROR_MESSAGE_LIMIT]


async def record_dead_letter(
    factory: async_sessionmaker[AsyncSession], *, queue: str, job_name: str, job_id: str,
    args: Sequence[Any], kwargs: dict[str, Any], exc: BaseException, attempts: int,
) -> None:
    """Upsert the dead-letter row for ``(queue, job_id)`` in its own short transaction (commit included).

    A conflicting row (job failed again after a replay) keeps ``first_failed_at`` and updates the rest.
    """
    refs = replayable_refs(args, kwargs)
    values = {
        "id": uuid7(), "queue": queue, "job_name": job_name[:128], "job_id": job_id[:256],
        "args_digest": args_digest(args, kwargs), "args_ref": refs, "replayable": refs is not None,
        "error_class": type(exc).__name__[:128], "error_message": scrub_message(str(exc)), "attempts": attempts,
    }
    statement = pg_insert(WorkerDeadLetter).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_worker_dead_letters_queue_job",
        set_={
            "error_class": statement.excluded.error_class, "error_message": statement.excluded.error_message,
            "attempts": statement.excluded.attempts, "last_failed_at": func.now(), "replayed_at": None,
            "args_digest": statement.excluded.args_digest, "args_ref": statement.excluded.args_ref,
            "replayable": statement.excluded.replayable,
        },
    )
    async with factory() as session:
        await session.execute(statement)
        await session.commit()


TIMEOUT_MARGIN_SECONDS = 0.5


def _arq_will_run_again(exc: BaseException, attempt: int, max_tries: int, timed_out: bool) -> bool:
    """Mirror arq 0.28 ``run_job``: would the job be picked up again after raising ``exc`` on ``attempt``?

    arq retries only ``Retry`` / ``RetryJob`` and a non-timeout ``CancelledError`` (worker shutdown), and
    only while ``attempt < max_tries``; any other exception, and a ``wait_for`` timeout (seen inside the
    job as ``CancelledError``), finishes the job permanently on the spot.
    """
    if attempt >= max_tries or timed_out:
        return False
    return isinstance(exc, (Retry, RetryJob, asyncio.CancelledError))


def dead_letter(
    function: Callable[..., Coroutine[Any, Any, Any]], *, max_tries: int, queue: str,
    timeout_seconds: float | None = None,
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Wrap an arq job so every attempt after which arq will not run it again lands in ``worker_dead_letters``.

    That is: any ordinary exception on any try (arq never retries those), a timeout (detected by elapsed
    wall time >= ``timeout_seconds`` when the job is cancelled), and a ``Retry``/``RetryJob``/shutdown
    cancellation on the final try (``ctx["job_try"] >= max_tries``). ``max_tries`` must equal the
    effective arq limit and ``timeout_seconds`` the effective ``job_timeout`` for this function. The
    original exception is always re-raised, recording errors are logged and swallowed, and without a
    session factory in ``ctx`` the wrapper is a pass-through. ``functools.wraps`` keeps arq's name.
    """
    @wraps(function)
    async def guarded(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Run the job; persist a dead-letter row when arq will not retry the failure."""
        started = time.monotonic()
        try:
            return await function(ctx, *args, **kwargs)
        except (Exception, asyncio.CancelledError) as exc:
            raw_attempt = ctx.get("job_try")
            attempt = raw_attempt if isinstance(raw_attempt, int) else 1
            factory = ctx.get("session_factory")
            timed_out = (
                isinstance(exc, asyncio.CancelledError) and timeout_seconds is not None
                and time.monotonic() - started >= timeout_seconds - TIMEOUT_MARGIN_SECONDS
            )
            if factory is not None and not _arq_will_run_again(exc, attempt, max_tries, timed_out):
                try:
                    await asyncio.shield(record_dead_letter(
                        factory, queue=queue, job_name=str(function.__name__),
                        job_id=str(ctx.get("job_id", "")), args=args, kwargs=kwargs, exc=exc, attempts=attempt,
                    ))
                except Exception:  # noqa: BLE001 - never mask the job's own failure
                    _log.warning("dead-letter record failed", exc_info=True)
            raise

    return guarded


@dataclass(frozen=True)
class ReplayPlan:
    """What a replay run did or would do: rows that can be re-enqueued and rows that need manual action."""

    replayable: list[WorkerDeadLetter]
    skipped: list[WorkerDeadLetter]
    executed: bool


async def list_dead_letters(
    factory: async_sessionmaker[AsyncSession], *, limit: int = 50,
) -> list[WorkerDeadLetter]:
    """Return the newest ``limit`` (max 500) dead letters, most recent failure first (read-only)."""
    async with factory() as session:
        result = await session.scalars(
            select(WorkerDeadLetter).order_by(WorkerDeadLetter.last_failed_at.desc()).limit(max(1, min(limit, 500)))
        )
        return list(result)


async def replay_dead_letters(
    factory: async_sessionmaker[AsyncSession], redis: Any, *, ids: Sequence[UUID] = (), execute: bool = False,
) -> ReplayPlan:
    """Plan (default) or perform re-enqueueing of un-replayed, replayable dead letters.

    Dry run reads only. With ``execute`` each row is re-enqueued under the fresh job id
    ``dlq:<id>:<replay_count+1>`` on its original queue and marked ``replayed_at``/``replay_count`` in
    one commit per row, so a crash never replays a row twice without a record. Rows without stored ids
    are returned in ``skipped`` and never touched.
    """
    async with factory() as session:
        query = select(WorkerDeadLetter).where(WorkerDeadLetter.replayed_at.is_(None))
        if ids:
            query = query.where(WorkerDeadLetter.id.in_(list(ids)))
        rows = list(await session.scalars(query.order_by(WorkerDeadLetter.first_failed_at).limit(500)))
        ready = [row for row in rows if row.replayable and row.args_ref is not None]
        skipped = [row for row in rows if row not in ready]
        if execute:
            for row in ready:
                await redis.enqueue_job(
                    row.job_name, *(row.args_ref or []), _job_id=f"dlq:{row.id}:{row.replay_count + 1}",
                    _queue_name=row.queue,
                )
                row.replayed_at = datetime.now(UTC)
                row.replay_count += 1
                await session.commit()
    return ReplayPlan(ready, skipped, execute)


async def purge_dead_letters(
    factory: async_sessionmaker[AsyncSession], *, older_than: timedelta, execute: bool = False,
) -> int:
    """Count (dry run) or delete dead letters whose last failure is older than ``older_than``; returns the count."""
    cutoff = datetime.now(UTC) - older_than
    async with factory() as session:
        stale = select(WorkerDeadLetter.id).where(WorkerDeadLetter.last_failed_at < cutoff)
        ids = list(await session.scalars(stale.limit(10_000)))
        if execute and ids:
            from sqlalchemy import delete

            await session.execute(delete(WorkerDeadLetter).where(WorkerDeadLetter.id.in_(ids)))
            await session.commit()
        return len(ids)


def _parser() -> argparse.ArgumentParser:
    """Build the ``list`` / ``replay`` / ``purge`` argument parser (dry run unless ``--execute``)."""
    parser = argparse.ArgumentParser(prog="python -m core.worker_dlq", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="show recent dead letters")
    listing.add_argument("--limit", type=int, default=50)
    replay = sub.add_parser("replay", help="re-enqueue replayable dead letters (dry run by default)")
    replay.add_argument("--id", dest="ids", action="append", type=UUID, default=[])
    replay.add_argument("--execute", action="store_true")
    purge = sub.add_parser("purge", help="delete old dead letters (dry run by default)")
    purge.add_argument("--older-than-days", type=int, required=True)
    purge.add_argument("--execute", action="store_true")
    return parser


def _format(row: WorkerDeadLetter) -> str:
    """One-line, secret-free description of a dead letter."""
    return (f"{row.id} {row.queue} {row.job_name} tries={row.attempts} replayable={row.replayable} "
            f"replayed={row.replay_count} last={row.last_failed_at:%Y-%m-%dT%H:%M:%SZ} "
            f"{row.error_class}: {row.error_message}")


async def _run(arguments: argparse.Namespace) -> int:
    """Open the engine (and Redis for replay), run the chosen subcommand, always dispose connections."""
    from arq.connections import RedisSettings, create_pool

    from core.config import Settings
    from core.database import make_session_factory

    settings = Settings()
    engine, factory = make_session_factory(settings.database_url, pool_size=1, max_overflow=0)
    pool = None
    try:
        if arguments.command == "list":
            for row in await list_dead_letters(factory, limit=arguments.limit):
                print(_format(row))
        elif arguments.command == "replay":
            if arguments.execute:
                pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            plan = await replay_dead_letters(factory, pool, ids=arguments.ids, execute=arguments.execute)
            verb = "replayed" if plan.executed else "would replay"
            for row in plan.replayable:
                print(f"{verb}: {_format(row)}")
            for row in plan.skipped:
                print(f"skipped (no stored ids): {_format(row)}")
            if not plan.executed:
                print("dry run; pass --execute to enqueue")
        else:
            count = await purge_dead_letters(
                factory, older_than=timedelta(days=arguments.older_than_days), execute=arguments.execute)
            print(f"{'deleted' if arguments.execute else 'would delete'} {count} dead letters")
        return 0
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
