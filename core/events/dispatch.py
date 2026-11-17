"""Outbox dispatch core: identity-only discovery and admitted durable claims, with Redis outside SQL locks.

Moved from ``modules.ingestion.dispatcher`` with identical semantics (keyset scan by ``(created_at, id)``,
``FOR UPDATE SKIP LOCKED`` claims, ``queued`` stale reclaim, quarantine to ``failed``, at-least-once delivery
and unchanged ARQ job ids). Event-type knowledge (job names, producers, envelope shapes, per-type dispatch
checks) comes from the descriptor-driven ``core.events.registry``; scope resolution comes from the declared
``OutboxScopeRuntime``.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.errors import DomainError
from core.events.model import EventOutbox
from core.events.registry import get_event_registry
from core.heavy_work.slots import HEAVY_JOB_MAX_TRIES, MAX_OPERATION_SECONDS
from core.worker_context import worker_session_factory
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import InternalJobScope

DISPATCH_STALE_AFTER = timedelta(seconds=30)
# Heavy-slot jobs retry on slot contention (>= ~435 s of minimum backoff over HEAVY_JOB_MAX_TRIES) and then hold the
# slot up to MAX_OPERATION_SECONDS, all under the same claim. Re-claiming after 30 s would enqueue a duplicate job id
# and void the original's claim check, so their reclaim window must outlast that worst case.
HEAVY_DISPATCH_JOBS = frozenset({"process_uploaded_file", "process_graph_operation"})
HEAVY_STALE_AFTER = timedelta(
    seconds=2.5 + 5 + 7.5 * (HEAVY_JOB_MAX_TRIES - 2) + MAX_OPERATION_SECONDS + 315
)
DISPATCH_SCAN_STATE = "ingestion_dispatch_scan_state"


def stale_after_for(job_name: str) -> timedelta:
    """Return how long a ``queued`` claim for ``job_name`` is trusted before the dispatcher may re-claim it."""
    return HEAVY_STALE_AFTER if job_name in HEAVY_DISPATCH_JOBS else DISPATCH_STALE_AFTER


def stale_queued_clause(now: datetime) -> Any:
    """SQL predicate for ``queued`` rows whose claim is older than their own job's reclaim window.

    Heavy-slot event types use ``HEAVY_STALE_AFTER``; all others keep ``DISPATCH_STALE_AFTER``. A row that was
    never stamped (``dispatched_at`` NULL) is always reclaimable.
    """
    routes = get_event_registry().routes
    heavy_types = sorted(
        name for name, route in routes.items() if route.job_name in HEAVY_DISPATCH_JOBS
    )
    stale = or_(
        EventOutbox.dispatched_at.is_(None),
        and_(
            EventOutbox.type.not_in(heavy_types),
            EventOutbox.dispatched_at < now - DISPATCH_STALE_AFTER,
        ),
        and_(
            EventOutbox.type.in_(heavy_types), EventOutbox.dispatched_at < now - HEAVY_STALE_AFTER
        ),
    )
    return and_(EventOutbox.status == "queued", stale)


def valid_event_envelope(event: EventOutbox | Any, scope: InternalJobScope) -> bool:
    """Validate the retained type/version/producer/payload container, then delegate to the type's handler.

    Unknown types, a version other than the integer ``1``, a producer outside the type's declared producers
    or a non-dict payload are rejected before any handler runs. No coercion, metadata promotion, default actor
    or current membership upgrade occurs.
    """
    registry = get_event_registry()
    route = registry.routes.get(event.type)
    if (
        route is None
        or type(event.version) is not int
        or event.version != 1
        or event.producer not in route.producers
        or not isinstance(event.payload, dict)
    ):
        return False
    return registry.handler(event.type).valid_envelope(event, scope)


async def _quarantine_event_identity(
    session: AsyncSession,
    event_id: UUID,
    status: str,
    dispatched_at: datetime | None,
) -> None:
    """Fail one unresolved identity using original status/time CAS without exposing its body.

    This internal dispatcher repair grants no read/domain authority and cannot overwrite
    an intervening claim. A malformed retained principal never becomes owner1.
    """
    await session.execute(
        update(EventOutbox)
        .where(
            EventOutbox.id == event_id,
            EventOutbox.status == status,
            EventOutbox.dispatched_at == dispatched_at,
        )
        .values(status="failed")
    )
    await session.commit()


def _dispatch_scan_cursor(value: object) -> tuple[datetime, UUID, datetime] | None:
    """Decode a scheduling-only full-key checkpoint; malformed values restart a sweep.

    The fixed creation-time ceiling makes each sweep finite despite newer arrivals.
    This checkpoint carries no payload, principal, claim or authorization.
    """
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    if any(not isinstance(item, str) or len(item) > 64 for item in value):
        return None
    try:
        stamp, identifier, ceiling = (
            datetime.fromisoformat(value[0]),
            UUID(value[1]),
            datetime.fromisoformat(value[2]),
        )
        if (
            stamp.tzinfo is None
            or stamp.utcoffset() is None
            or ceiling.tzinfo is None
            or ceiling.utcoffset() is None
            or str(identifier) != value[1]
            or stamp > ceiling
            or ceiling > datetime.now(UTC)
        ):
            return None
        return stamp.astimezone(UTC), identifier, ceiling.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class _DispatchClaim:
    """Mutable record of one dispatch attempt, filled as the claim transaction progresses.

    It is read by the failure handler even when the claim transaction raised after setting ``claimed_at``
    (for example a failed commit), so the exact original attempt can be returned to ``pending``.
    """

    claimed_at: datetime | None = None
    job_name: str = ""
    scope: InternalJobScope | None = None
    access_fence: Any = None


async def _reserve_dispatch_page(
    ctx: dict[str, object],
    factory: async_sessionmaker[AsyncSession],
) -> tuple[list[Any], datetime]:
    """Reserve one bounded page (at most 100 identities) of dispatchable events and advance the shared cursor.

    Runs under the shared scheduling lock so overlapping cron calls cannot regress or race a sweep wrap. The
    checkpoint advances before any per-job admission, even for identities that are later denied or skipped. An
    empty page wraps the sweep once (at most 100 candidates across both reads). Redis and original-subject
    admission happen after the lock is released. Returns the identities and the evaluation instant ``now``
    that also governs the per-identity lease comparison.
    """
    scan_state = ctx.get(DISPATCH_SCAN_STATE)
    if (
        not isinstance(scan_state, dict)
        or set(scan_state) != {"cursor", "lock"}
        or not isinstance(scan_state["lock"], asyncio.Lock)
    ):
        raise TypeError("Worker startup must supply shared ingestion dispatch scheduling state")
    async with scan_state["lock"]:
        now = datetime.now(UTC)
        cursor = _dispatch_scan_cursor(scan_state["cursor"])
        if cursor is None:
            scan_state["cursor"] = None
        ceiling = cursor[2] if cursor is not None else now
        async with factory() as session:
            statement = select(
                EventOutbox.id,
                EventOutbox.status,
                EventOutbox.dispatched_at,
                EventOutbox.created_at,
            ).where(
                EventOutbox.type.in_(list(get_event_registry().routes)),
                or_(
                    and_(EventOutbox.status == "pending", EventOutbox.next_attempt_at <= now),
                    stale_queued_clause(now),
                ),
            )
            page = statement.where(EventOutbox.created_at <= ceiling)
            if cursor is not None:
                page = page.where(
                    or_(
                        EventOutbox.created_at > cursor[0],
                        and_(
                            EventOutbox.created_at == cursor[0],
                            EventOutbox.id > cursor[1],
                        ),
                    )
                )
            identities = (
                await session.execute(
                    page.order_by(EventOutbox.created_at, EventOutbox.id).limit(100)
                )
            ).all()
            if not identities and cursor is not None:
                # Only an empty page wraps; at most100 candidate identities across both reads.
                ceiling = now
                scan_state["cursor"] = None
                identities = (
                    await session.execute(
                        statement.where(EventOutbox.created_at <= ceiling)
                        .order_by(EventOutbox.created_at, EventOutbox.id)
                        .limit(100)
                    )
                ).all()
        if identities:
            last_identity = identities[-1]
            scan_state["cursor"] = (
                last_identity.created_at.astimezone(UTC).isoformat(),
                str(last_identity.id),
                ceiling.isoformat(),
            )
    return list(identities), now


async def _claim_for_dispatch(
    factory: async_sessionmaker[AsyncSession],
    claim: _DispatchClaim,
    identifier: UUID,
    original_status: str,
    original_dispatch: datetime | None,
    *,
    now: datetime,
    enabled: bool,
) -> bool:
    """Admit one original subject and commit the ``queued`` dispatch claim; ``False`` means nothing to enqueue.

    Resolves the retained scope (unresolvable identities are quarantined), rechecks the original access fence,
    locks the outbox row with ``SKIP LOCKED`` against the exact original status/timestamp, validates the
    envelope and lease, then stamps ``claim`` and commits before Redis is touched. ``claim.claimed_at`` is set
    just before the commit so a failed commit is still visible to the recovery handler.
    """
    async with factory() as session:
        registry = get_event_registry()
        scope = await registry.runtime().resolve_scope(
            session, identifier, multi_workspace_enabled=enabled
        )
        if scope is None:
            await session.rollback()
            await _quarantine_event_identity(
                session, identifier, original_status, original_dispatch
            )
            return False
        claim.scope = scope
        claim.access_fence = await read_access_fence(
            session, scope=scope, multi_workspace_enabled=enabled
        )
        event = await session.scalar(
            select(EventOutbox)
            .where(
                EventOutbox.id == identifier,
                EventOutbox.workspace_id == scope.workspace_id,
                EventOutbox.actor_user_id == scope.actor_user_id,
                EventOutbox.membership_revision == scope.membership_revision,
                EventOutbox.status == original_status,
                EventOutbox.dispatched_at == original_dispatch,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if event is None:
            return False
        if not valid_event_envelope(event, scope):
            event.status = "failed"
            await session.commit()
            return False
        if not await registry.handler(event.type).validate_dispatch(
            session, event, scope, now=now, enabled=enabled
        ):
            return False
        claim.claimed_at = datetime.now(UTC)
        event.status = "queued"
        event.dispatched_at = claim.claimed_at
        claim.job_name = registry.routes[event.type].job_name
        await session.commit()
    return True


async def _return_claim_to_pending(
    factory: async_sessionmaker[AsyncSession],
    identifier: UUID,
    claim: _DispatchClaim,
    *,
    enabled: bool,
) -> None:
    """After a failed enqueue, return only the exact original attempt to ``pending`` under current admission.

    The queued row stays durable after a lost enqueue reply and the stale scan recovers it; this compare-and-set
    merely speeds that up. A changed scope or lost authorization does nothing.
    """
    try:
        async with factory() as session:
            current_scope = (
                await get_event_registry()
                .runtime()
                .resolve_scope(session, identifier, multi_workspace_enabled=enabled)
            )
            if current_scope != claim.scope:
                return
            # claimed_at is set only after scope resolved, so scope is non-None here.
            owner_scope = cast(InternalJobScope, claim.scope)
            await lock_access_fence(
                session,
                scope=owner_scope,
                expected=claim.access_fence,
                multi_workspace_enabled=enabled,
            )
            await session.execute(
                update(EventOutbox)
                .where(
                    EventOutbox.id == identifier,
                    EventOutbox.workspace_id == owner_scope.workspace_id,
                    EventOutbox.actor_user_id == owner_scope.actor_user_id,
                    EventOutbox.membership_revision == owner_scope.membership_revision,
                    EventOutbox.status == "queued",
                    EventOutbox.dispatched_at == claim.claimed_at,
                )
                .values(status="pending", next_attempt_at=datetime.now(UTC) + DISPATCH_STALE_AFTER)
            )
            await session.commit()
    except (HTTPException, DomainError):
        return


async def dispatch_pending_work(ctx: dict[str, object]) -> int:
    """Scan at most100 identities, admit each original subject and commit before Redis.

    The committed queued timestamp is the dispatch claim. Recovery and failure compare
    that exact timestamp under current original admission, so successor attempts survive.
    No SQL locks span enqueue; absent Redis acknowledgements remain recoverable durable rows.
    Malformed envelopes quarantine; lost access never acquires another actor or epoch.
    A shared nested ctx full-key checkpoint advances even on denied/skipped identities.
    ARQ copies invocation ctx, so startup owns this shared scheduling dict and lock.
    The lock only reserves one bounded identity page and advances its checkpoint before
    per-job admission; overlapping cron calls cannot regress or race a sweep wrap.
    Each finite creation-time sweep wraps once after exhaustion, revisiting its prefix;
    worker restart resets scheduling only, with no durability or admission claim.

    Phases: ``_reserve_dispatch_page`` (under the scheduling lock) -> per identity ``_claim_for_dispatch``
    (committed claim) -> Redis enqueue -> on failure ``_return_claim_to_pending``.
    """
    factory = worker_session_factory(ctx)
    redis = cast(ArqRedis, ctx["redis"])
    enabled = cast(Settings, ctx["settings"]).multi_workspace_enabled
    if type(enabled) is not bool:
        raise TypeError("Actual workspace rollout flag required")
    identities, now = await _reserve_dispatch_page(ctx, factory)
    # Scheduling lock is released before any original-subject admission, mutation or Redis.
    enqueued = 0
    for identifier, original_status, original_dispatch, _created_at in identities:
        claim = _DispatchClaim()
        try:
            if not await _claim_for_dispatch(
                factory,
                claim,
                identifier,
                original_status,
                original_dispatch,
                now=now,
                enabled=enabled,
            ):
                continue
            claimed_at = cast(datetime, claim.claimed_at)
            # Redis carries only identity; each consumer re-resolves the durable envelope.
            job = await redis.enqueue_job(
                claim.job_name,
                str(identifier),
                _job_id=f"ingestion:{identifier}:{claimed_at.isoformat()}",
                _defer_until=claimed_at,
            )
            if job is not None:
                enqueued += 1
        except (HTTPException, DomainError):
            # Lost original authorization cannot expose payload or repair a successor claim.
            continue
        except Exception:
            # The queued row remains durable after a lost enqueue reply; the stale scan recovers it.
            # Only an exact original attempt can return to pending now.
            if claim.claimed_at is None:
                raise
            await _return_claim_to_pending(factory, identifier, claim, enabled=enabled)
    return enqueued
