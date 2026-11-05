"""Dependency-free cron scheduling primitives shared by the worker processes.

Four independent concerns live here, stdlib-only apart from the Prometheus recorder in ``core.metrics``
(Redis is passed in and used through duck typing):

* **Deterministic offsets** - ``stable_offset`` / ``second_set`` / ``minute_set`` derive a per-job phase
  from a hash of the job name so a fleet of jobs with the same period never fires in the same second.
  The period of every job is preserved; only its phase moves. The mapping is stable across restarts and
  replicas, so all replicas agree on when a job is due.
* **Single-flight lease** - ``single_flight`` wraps a cron coroutine in a Redis ``SET NX PX`` lease with a
  random token. Overlapping runs of the same job (a run longer than its period) and multiple worker
  replicas therefore never execute one cron concurrently. The lease is renewed while the job runs and
  released (compare-and-delete) in ``finally``; a crashed holder is replaced when the TTL lapses.
* **Run bookkeeping** - ``record_cron_run(name, duration, ok)`` keeps last-success / last-failure
  timestamps and duration in process memory (``cron_status`` is the read API for observability),
  publishes the same outcome to the Prometheus cron series (``core.metrics.record_cron``) and
  ``persist_cron_run`` mirrors them to a Redis hash so a restarted worker can see missed runs.
* **Catch-up and wake-ups** - ``overdue_jobs`` lists jobs whose last success is older than twice their
  period; ``wake`` / ``notify_on_commit`` let producers trigger a poller job immediately after the row
  that needs processing is committed, which lets the poll period become a slow safety net. A wake that
  meets a held lease leaves a rerun flag so the holder runs once more.

Nothing here is authority: leases and timestamps are best-effort scheduling aids; every job remains
idempotent against its durable rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any

from core.metrics import record_cron

_log = logging.getLogger(__name__)

LEASE_PREFIX = "cron:lease:"
LAST_SUCCESS_HASH = "cron:last_success"
CRON_QUEUE = "arq:cron"
WAKE_JOB_PREFIX = "wake:"
WAKE_FLAG_PREFIX = "cron:wake:"
RERUN_FLAG_PREFIX = "cron:rerun:"
RUN_CRON_FUNCTION = (
    "run_cron_job"  # plain arq function (not a CronJob) that executes a named cron job
)
WAKE_FLAG_TTL_MS = 5_000
RERUN_FLAG_TTL_MS = 60_000
MAX_RERUNS = 3
LEASE_MIN_TTL_MS = 30_000
LEASE_MAX_TTL_MS = 300_000
CATCH_UP_FACTOR = 2

# Compare-and-delete / compare-and-pexpire so a holder never releases or extends a successor's lease.
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_RENEW_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"


def stable_offset(name: str, modulus: int, salt: str = "") -> int:
    """Return a deterministic integer in ``[0, modulus)`` derived from the job name (SHA-256, not ``hash``).

    Python's built-in ``hash`` is randomized per process, which would give each replica a different
    phase; SHA-256 is identical everywhere. ``salt`` yields independent offsets for the same name.
    """
    if modulus <= 0:
        raise ValueError("modulus must be positive")
    digest = hashlib.sha256(f"{salt}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def second_set(name: str, every_seconds: int) -> set[int]:
    """Seconds-of-minute for a job that runs every ``every_seconds`` (a divisor of 60) at a hashed phase."""
    if every_seconds <= 0 or 60 % every_seconds:
        raise ValueError("every_seconds must divide 60")
    phase = stable_offset(name, every_seconds, "sec-phase")
    return set(range(phase, 60, every_seconds))


def minute_set(name: str, every_minutes: int) -> set[int]:
    """Minutes-of-hour for a job that runs every ``every_minutes`` (a divisor of 60) at a hashed phase."""
    if every_minutes <= 0 or 60 % every_minutes:
        raise ValueError("every_minutes must divide 60")
    phase = stable_offset(name, every_minutes, "min-phase")
    return set(range(phase, 60, every_minutes))


def second_of_minute(name: str) -> int:
    """Hashed second inside the minute used to de-synchronise minute-or-longer schedules."""
    return stable_offset(name, 60, "sec-in-minute")


def lease_ttl_ms(period_seconds: int) -> int:
    """Lease TTL for a job period: long enough to survive a missed renewal, short enough to recover quickly.

    Twice the period, clamped to ``[30 s, 300 s]``. The holder renews at a third of the TTL, so a live
    holder never loses the lease; a dead one is replaced within the TTL.
    """
    return max(LEASE_MIN_TTL_MS, min(LEASE_MAX_TTL_MS, period_seconds * 2 * 1000))


async def acquire_lease(redis: Any, name: str, ttl_ms: int) -> str | None:
    """Try ``SET key token NX PX ttl``; return the holder token, or ``None`` when another run holds it."""
    token = secrets.token_hex(16)
    acquired = await redis.set(LEASE_PREFIX + name, token, nx=True, px=ttl_ms)
    return token if acquired else None


async def renew_lease(redis: Any, name: str, token: str, ttl_ms: int) -> bool:
    """Extend the lease only while ``token`` still owns it; False means the lease was lost."""
    return bool(await redis.eval(_RENEW_LUA, 1, LEASE_PREFIX + name, token, ttl_ms))


async def release_lease(redis: Any, name: str, token: str) -> bool:
    """Delete the lease only if ``token`` still owns it; never removes a successor's lease."""
    return bool(await redis.eval(_RELEASE_LUA, 1, LEASE_PREFIX + name, token))


async def _renew_loop(redis: Any, name: str, token: str, ttl_ms: int) -> None:
    """Renew the lease every ``ttl/3`` until cancelled; stop (and warn) once the lease is lost or Redis fails."""
    interval = ttl_ms / 3000
    while True:
        await asyncio.sleep(interval)
        try:
            if not await renew_lease(redis, name, token, ttl_ms):
                _log.warning("cron lease lost while running", extra={"cron_job": name})
                return
        except Exception:  # noqa: BLE001 - renewal is best-effort; the TTL bounds the damage
            _log.warning("cron lease renewal failed", extra={"cron_job": name}, exc_info=True)
            return


@dataclass(frozen=True)
class CronRun:
    """Last observed outcome of one cron job in this process (monotonic-free wall-clock epoch seconds)."""

    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_duration_seconds: float | None = None
    successes: int = 0
    failures: int = 0
    skipped: int = 0


_RUNS: dict[str, CronRun] = {}


def record_cron_run(name: str, duration: float, ok: bool, *, now: float | None = None) -> CronRun:
    """Record one finished run (success or failure) for ``name`` and return the new in-process state.

    This is the single per-run hook for every lease-guarded execution (scheduled tick, wake-up, catch-up),
    so it also publishes the Prometheus cron series via ``core.metrics.record_cron``; that recorder
    isolates its own errors, so metrics can never fail a job. Lease-held skips are not runs and go to
    ``record_cron_skip`` instead. ``duration`` is wall seconds. Not durable; see ``persist_cron_run``
    for the Redis mirror used by catch-up.
    """
    stamp = time.time() if now is None else now
    previous = _RUNS.get(name, CronRun())
    _RUNS[name] = CronRun(
        last_success_at=stamp if ok else previous.last_success_at,
        last_failure_at=previous.last_failure_at if ok else stamp,
        last_duration_seconds=duration,
        successes=previous.successes + (1 if ok else 0),
        failures=previous.failures + (0 if ok else 1),
        skipped=previous.skipped,
    )
    record_cron(name, duration, ok)
    return _RUNS[name]


def record_cron_skip(name: str) -> None:
    """Count a run skipped because another holder owned the lease (observability only)."""
    previous = _RUNS.get(name, CronRun())
    _RUNS[name] = CronRun(
        previous.last_success_at,
        previous.last_failure_at,
        previous.last_duration_seconds,
        previous.successes,
        previous.failures,
        previous.skipped + 1,
    )


def cron_status() -> dict[str, CronRun]:
    """Return a copy of the per-job run state for the observability package."""
    return dict(_RUNS)


def reset_cron_status() -> None:
    """Clear the in-process run state (tests and worker restarts)."""
    _RUNS.clear()


async def persist_cron_run(
    redis: Any, name: str, duration: float, ok: bool, *, now: float | None = None
) -> None:
    """Mirror a successful run to ``cron:last_success`` as ``"<epoch>:<duration_ms>"``; failures are not stored.

    Best-effort: a Redis error is logged and ignored so bookkeeping can never fail a job.
    """
    if not ok:
        return
    stamp = time.time() if now is None else now
    try:
        await redis.hset(LAST_SUCCESS_HASH, name, f"{stamp:.3f}:{int(duration * 1000)}")
    except Exception:  # noqa: BLE001 - bookkeeping must not fail the job
        _log.warning("cron last-success persist failed", extra={"cron_job": name}, exc_info=True)


async def read_last_success(redis: Any) -> dict[str, float]:
    """Return ``{job name: last success epoch seconds}`` from Redis; malformed entries are ignored."""
    raw: Mapping[Any, Any] = await redis.hgetall(LAST_SUCCESS_HASH) or {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        text_key = key.decode() if isinstance(key, bytes) else str(key)
        text_value = value.decode() if isinstance(value, bytes) else str(value)
        with contextlib.suppress(ValueError):
            result[text_key] = float(text_value.split(":", 1)[0])
    return result


def overdue_jobs(
    periods: Mapping[str, int],
    last_success: Mapping[str, float],
    *,
    now: float | None = None,
) -> list[str]:
    """Names whose last success is missing or older than ``CATCH_UP_FACTOR`` x period (sorted, deterministic)."""
    stamp = time.time() if now is None else now
    return sorted(
        name
        for name, period in periods.items()
        if name not in last_success or stamp - last_success[name] > CATCH_UP_FACTOR * period
    )


async def _run_under_lease(
    function: Callable[..., Coroutine[Any, Any, Any]],
    redis: Any,
    name: str,
    ttl: int,
    ctx: dict[str, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[bool, Any]:
    """Run ``function`` once while holding the lease; return ``(ran, result)`` (``ran`` False = lease held).

    The lease is renewed while the job runs and released (compare-and-delete) in ``finally``; duration
    and outcome go to ``record_cron_run`` / ``persist_cron_run``; exceptions propagate after bookkeeping.
    """
    token: str | None = None
    renewer: asyncio.Task[None] | None = None
    if redis is not None:
        token = await acquire_lease(redis, name, ttl)
        if token is None:
            record_cron_skip(name)
            return False, None
        renewer = asyncio.get_running_loop().create_task(_renew_loop(redis, name, token, ttl))
    started = time.monotonic()
    ok = False
    try:
        result = await function(ctx, *args, **kwargs)
        ok = True
        return True, result
    finally:
        duration = time.monotonic() - started
        if renewer is not None:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
        record_cron_run(name, duration, ok)
        if redis is not None and token is not None:
            await persist_cron_run(redis, name, duration, ok)
            with contextlib.suppress(Exception):  # TTL expiry covers a failed release
                await asyncio.shield(release_lease(redis, name, token))


async def _rerun_requested(redis: Any, name: str) -> bool:
    """Consume the 'rerun requested' flag; True when a wake arrived while the lease was held."""
    try:
        return bool(await redis.delete(RERUN_FLAG_PREFIX + name))
    except Exception:  # noqa: BLE001 - a lost rerun is covered by the safety-net schedule
        return False


def single_flight(
    function: Callable[..., Coroutine[Any, Any, Any]],
    *,
    name: str,
    period_seconds: int,
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Wrap a cron coroutine so at most one run per ``name`` executes cluster-wide.

    The wrapper acquires the Redis lease (``ctx["redis"]``) and returns ``None`` immediately when another
    holder owns it. A *wake-up* call (``ctx["cron_wake"]`` true) that finds the lease held sets a
    'rerun requested' flag instead of being lost; the holder checks the flag after releasing the lease
    and runs again (at most ``MAX_RERUNS`` extra times per call), so work committed during a run is
    picked up promptly. Because every skip happens before the holder's release and the flag is read
    after it, no wake can slip between the two. Without ``ctx["redis"]`` (unit contexts) the job simply
    runs. ``functools.wraps`` keeps the original ``__qualname__`` so arq's ``cron:<qualname>`` job name
    is unchanged.
    """
    ttl = lease_ttl_ms(period_seconds)

    @wraps(function)
    async def guarded(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``function`` only while holding the per-job lease; record a rerun when a wake is skipped."""
        redis = ctx.get("redis")
        result: Any = None
        for _attempt in range(MAX_RERUNS + 1):
            ran, result = await _run_under_lease(function, redis, name, ttl, ctx, args, kwargs)
            if not ran:
                if ctx.get("cron_wake") and redis is not None:
                    with contextlib.suppress(Exception):
                        await redis.set(RERUN_FLAG_PREFIX + name, "1", px=RERUN_FLAG_TTL_MS)
                return None
            if redis is None or not await _rerun_requested(redis, name):
                break
        return result

    return guarded


# --- event-driven wake-ups ---------------------------------------------------------------------------

_WAKE_HOOK: Callable[..., Awaitable[None]] | None = None


async def _run_hook(
    hook: Callable[..., Awaitable[None]], job_name: str, defer_seconds: float = 1.0
) -> None:
    """Await the wake hook as a task body, swallowing errors (a lost wake falls back to the safety-net poll)."""
    with contextlib.suppress(Exception):
        await hook(job_name, defer_seconds)


def set_wake_hook(hook: Callable[..., Awaitable[None]] | None) -> None:
    """Install (or clear) the process-wide coroutine ``hook(job_name, defer_seconds)`` that enqueues a wake-up."""
    global _WAKE_HOOK
    _WAKE_HOOK = hook


async def wake(redis: Any, job_name: str, *, defer_seconds: float = 1.0) -> bool:
    """Queue an immediate run of the poller cron ``job_name`` on the cron queue, debounced by a Redis flag.

    Wakes enqueue the plain arq function ``run_cron_job`` under a *unique* job id. A fixed id (or a
    ``cron:<name>`` CronJob id) would be blocked by arq's kept result / in-progress keys (60 s for
    CronJobs, the result TTL after a failure) and make wakes slower than the safety net. Bursts coalesce
    through ``SET cron:wake:<name> NX PX`` (cleared when the wake job starts, so a commit during the run
    queues the next one). Returns True when a new wake was queued. Never raises: a lost wake is covered
    by the slow safety-net schedule.
    """
    flag = WAKE_FLAG_PREFIX + job_name
    try:
        if not await redis.set(flag, "1", nx=True, px=WAKE_FLAG_TTL_MS):
            return False  # a wake is already pending; it will observe this commit too
        job = await redis.enqueue_job(
            RUN_CRON_FUNCTION,
            job_name,
            True,
            _job_id=f"{WAKE_JOB_PREFIX}{job_name}:{secrets.token_hex(6)}",
            _queue_name=CRON_QUEUE,
            _defer_by=defer_seconds,
        )
    except Exception:  # noqa: BLE001 - the periodic safety net still runs the job
        _log.warning("cron wake enqueue failed", extra={"cron_job": job_name}, exc_info=True)
        with contextlib.suppress(Exception):
            await redis.delete(flag)
        return False
    return job is not None


async def clear_wake_flag(redis: Any, job_name: str) -> None:
    """Drop the debounce flag when a wake job starts so later commits can queue the next wake (best-effort)."""
    with contextlib.suppress(Exception):
        await redis.delete(WAKE_FLAG_PREFIX + job_name)


def notify_on_commit(session: Any, job_name: str, *, defer_seconds: float = 1.0) -> None:
    """Wake ``job_name`` after ``session``'s outermost transaction commits (no-op without a wake hook).

    ``defer_seconds`` delays the wake for rows that only become due later (retry backoff,
    ``next_attempt_at``); repeated calls for one job in a transaction keep the smallest delay.

    Each session gets exactly one ``after_commit`` and one ``after_soft_rollback`` listener (installed
    on the first call, flagged in ``session.info``) that drain / discard the pending set, so rollbacks
    neither leave a stale listener that fires on a later commit nor stack duplicates. The wake is
    scheduled on the running loop so it never blocks or fails the commit. Call it where a durable row
    that the poller must process is added.
    """
    if _WAKE_HOOK is None:
        return
    from sqlalchemy import event  # local import keeps module import dependency-free

    sync_session = getattr(session, "sync_session", session)
    info = sync_session.info
    pending: dict[str, float] = info.setdefault("cron_wakes", {})
    pending[job_name] = min(defer_seconds, pending.get(job_name, defer_seconds))
    if info.get("cron_wake_listeners"):
        return
    info["cron_wake_listeners"] = True

    def _fire(_session: Any) -> None:
        """After a successful commit schedule one wake per pending job and clear the set."""
        wakes, hook = sorted(pending.items()), _WAKE_HOOK
        pending.clear()
        if hook is None:
            return
        for name, delay in wakes:
            with contextlib.suppress(RuntimeError):  # no running loop: nothing to schedule on
                asyncio.get_running_loop().create_task(_run_hook(hook, name, delay))

    def _discard(_session: Any, previous_transaction: Any = None) -> None:
        """Forget pending wakes when the outermost transaction rolled back (savepoint rollbacks keep them)."""
        if previous_transaction is None or getattr(previous_transaction, "parent", None) is None:
            pending.clear()

    event.listen(sync_session, "after_commit", _fire)
    event.listen(sync_session, "after_soft_rollback", _discard)
