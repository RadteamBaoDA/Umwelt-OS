"""Operator CLI that re-queues failed (quarantined) outbox events.

Usage::

    python -m core.events.replay --owner-user-id 1 --event-id <uuid>            # dry run (default)
    python -m core.events.replay --owner-user-id 1 --type document.version.ready --since 2026-11-01T00:00:00Z
    python -m core.events.replay --owner-user-id 1 --event-id <uuid> --execute  # actually re-queue

Only ``failed`` rows are eligible, and only rows whose principal is the named workspace owner (the row's
``actor_user_id`` must equal ``--owner-user-id`` and the workspace's ``owner_user_id``). Re-queuing sets the row
back to ``pending`` with ``next_attempt_at = now`` and a cleared ``dispatched_at``; the dispatcher then claims it
like a new event (fresh job id), re-validating the envelope, so a still-invalid event is quarantined again.
Destructive types (purge, cleanup, purge-cancelled stage events) need ``--allow-destructive``.
Without ``--execute`` nothing is written. Selection is bounded by ``--limit`` (default 100, max 1000).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.database import make_session_factory
from core.events.model import EventOutbox
from core.workspaces.models import Workspace

MAX_LIMIT = 1000
# Single source of truth: types whose replay is destructive (purge/cleanup) or would resurrect work a source
# purge cancelled (``modules/ingestion/_purge.py`` marks stage events of purged runs ``failed``).
DESTRUCTIVE_TYPES = frozenset({
    "source.purge.requested", "source.purge.coverage", "source.purge.progressed", "document.cleanup.requested",
    "ingestion.stage.requested", "connector.crawl.requested", "ingestion.normalize.requested",
})


def is_destructive(event_type: str) -> bool:
    """Return whether replaying ``event_type`` needs ``--allow-destructive`` (purge, cleanup, purge-cancelled)."""
    return event_type in DESTRUCTIVE_TYPES


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse and validate CLI arguments.

    Exits (``SystemExit(2)``) unless exactly one selector style is used: ``--event-id``, or ``--type`` together
    with ``--since``.
    """
    parser = argparse.ArgumentParser(prog="python -m core.events.replay", description=__doc__.split("\n\n")[0])
    parser.add_argument("--owner-user-id", type=int, required=True, help="Workspace owner whose events may be replayed")
    parser.add_argument("--event-id", type=UUID, help="Replay exactly one event")
    parser.add_argument("--type", dest="event_type", help="Replay failed events of this type (needs --since)")
    parser.add_argument("--since", type=_parse_since, help="ISO-8601 instant; only events created at/after it")
    parser.add_argument("--limit", type=int, default=100, help=f"Maximum rows (1..{MAX_LIMIT}, default 100)")
    parser.add_argument("--execute", action="store_true", help="Apply the re-queue (default is a dry run)")
    parser.add_argument("--allow-destructive", action="store_true",
                        help="Permit replaying purge/cleanup events and stage events a source purge cancelled")
    args = parser.parse_args(argv)
    if (args.event_id is None) == (args.event_type is None):
        parser.error("use exactly one of --event-id or --type/--since")
    if args.event_type is not None and args.since is None:
        parser.error("--type requires --since")
    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
    if args.owner_user_id <= 0:
        parser.error("--owner-user-id must be positive")
    return args


def _parse_since(value: str) -> datetime:
    """Parse an ISO-8601 instant (``Z`` accepted); naive values are rejected to avoid timezone guesses."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--since must include a timezone (e.g. 2026-11-01T00:00:00Z)")
    return parsed.astimezone(UTC)


async def select_replayable(
    session: AsyncSession, *, owner_user_id: int, event_id: UUID | None, event_type: str | None,
    since: datetime | None, limit: int,
) -> list[tuple[UUID, str, datetime]]:
    """Return ``(id, type, created_at)`` of failed events owned by ``owner_user_id``, oldest first.

    Read-only. The workspace join proves the row's principal is that workspace's owner. Bounded by ``limit``.
    """
    stmt = (select(EventOutbox.id, EventOutbox.type, EventOutbox.created_at)
            .join(Workspace, (Workspace.id == EventOutbox.workspace_id)
                  & (Workspace.owner_user_id == EventOutbox.actor_user_id))
            .where(EventOutbox.status == "failed", EventOutbox.actor_user_id == owner_user_id))
    if event_id is not None:
        stmt = stmt.where(EventOutbox.id == event_id)
    if event_type is not None:
        stmt = stmt.where(EventOutbox.type == event_type)
    if since is not None:
        stmt = stmt.where(EventOutbox.created_at >= since)
    rows = (await session.execute(stmt.order_by(EventOutbox.created_at, EventOutbox.id).limit(limit))).all()
    return [(row.id, row.type, row.created_at) for row in rows]


async def requeue(session: AsyncSession, event_ids: Sequence[UUID], *, now: datetime) -> int:
    """Move still-``failed`` rows back to ``pending`` and return how many changed (caller commits).

    The ``status == 'failed'`` compare-and-set makes this idempotent and safe against a concurrent replay.
    """
    if not event_ids:
        return 0
    result = await session.execute(update(EventOutbox).where(
        EventOutbox.id.in_(list(event_ids)), EventOutbox.status == "failed",
    ).values(status="pending", next_attempt_at=now, dispatched_at=None))
    return int(getattr(result, "rowcount", 0) or 0)


async def run(args: argparse.Namespace, session: AsyncSession) -> int:
    """Select, report and (only with ``--execute``) re-queue; returns the process exit code.

    One transaction: the selection and the update share ``session``; a dry run commits nothing.
    """
    found = await select_replayable(
        session, owner_user_id=args.owner_user_id, event_id=args.event_id, event_type=args.event_type,
        since=args.since, limit=args.limit)
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    destructive = 0
    for event_id, event_type, created_at in found:
        flag = " DESTRUCTIVE" if is_destructive(event_type) else ""
        destructive += bool(flag)
        print(f"[{mode}] {event_id} {event_type} status=failed created_at={created_at.isoformat()}{flag}")
    if destructive and args.execute and not args.allow_destructive:
        # Refuse the whole batch: a partial replay would hide which rows were skipped.
        print(f"[{mode}] refused: {destructive} destructive event(s); pass --allow-destructive to replay them")
        return 2
    if destructive and not args.execute and not args.allow_destructive:
        print(f"[{mode}] note: destructive events present; --execute will refuse without --allow-destructive")
    if not found:
        print(f"[{mode}] no failed events match")
        return 1 if args.event_id is not None else 0
    if not args.execute:
        print(f"[{mode}] {len(found)} event(s) would be re-queued; pass --execute to apply")
        return 0
    changed = await requeue(session, [item[0] for item in found], now=datetime.now(UTC))
    await session.commit()
    print(f"[{mode}] re-queued {changed} event(s)")
    return 0


async def _amain(argv: Sequence[str] | None) -> int:
    """Open a short-lived session from process settings and run the replay."""
    args = parse_args(argv)
    engine, factory = make_session_factory(Settings().database_url, pool_size=1, max_overflow=0)
    try:
        async with factory() as session:
            return await run(args, session)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (``python -m core.events.replay``)."""
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
