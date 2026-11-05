"""Cron schedule construction: hashed phase offsets plus the per-job single-flight lease.

``build_cron`` turns a ``CronSpec`` (function + period) into an arq ``CronJob``. The period is preserved
exactly; only the phase inside the period is derived from a stable hash of the job name
(``core.cron.stable_offset``) so jobs sharing a period do not all fire in the same second. Every job is
wrapped by ``core.cron.single_flight`` so overlapping runs and multiple worker replicas never execute
the same cron concurrently.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from arq.cron import CronJob
from arq.cron import cron as _cron

from core.cron import minute_set, second_of_minute, second_set, single_flight, stable_offset

HOUR = 3600
DAY = 86400


@dataclass(frozen=True)
class CronSpec:
    """One recurring job: ``period_seconds`` is the interval; ``minute``/``hour`` pin a fixed wall-clock slot.

    ``period_seconds`` must be a divisor of 60 s (sub-minute), a whole number of minutes dividing 60,
    one hour, or one day. ``minute`` (and ``hour`` for daily jobs) override the hashed slot when a job
    must stay at a documented time; the second inside the minute is still hashed.
    """

    function: Callable[..., Any]
    period_seconds: int
    run_at_startup: bool = False
    minute: int | None = None
    hour: int | None = None

    @property
    def name(self) -> str:
        """Stable job name used for hashing, the lease key and the last-success record."""
        return str(self.function.__name__)


def fire_times(spec: CronSpec) -> dict[str, Any]:
    """Return the arq ``cron()`` time keywords (``second``/``minute``/``hour``) for a spec.

    Sub-minute periods spread over a hashed phase of seconds; minute-scale periods use a hashed minute
    phase plus a hashed second; hourly jobs use a hashed (or pinned) minute; daily jobs need a pinned
    hour. Raises ``ValueError`` for unsupported periods so a bad schedule fails at import time.
    """
    period, name = spec.period_seconds, spec.name
    if period < 60:
        return {"second": second_set(name, period), "minute": None}
    second = second_of_minute(name)
    if period == 60:
        return {"second": second, "minute": None}
    if period < HOUR:
        return {"second": second, "minute": minute_set(name, period // 60)}
    minute = spec.minute if spec.minute is not None else stable_offset(name, 60, "minute")
    if period == HOUR:
        return {"second": second, "minute": minute}
    if period == DAY and spec.hour is not None:
        return {"second": second, "minute": minute, "hour": spec.hour}
    raise ValueError(f"unsupported cron period for {name}: {period}")


def guarded_function(spec: CronSpec, gate: Callable[[Callable[..., Any]], Callable[..., Any]]) -> Callable[..., Any]:
    """The lease-guarded coroutine for a spec: ``gate`` (module availability) inside, ``single_flight`` outside.

    Scheduled ticks, wake-ups and catch-up runs all call this same wrapper, so they share one lease.
    """
    return single_flight(gate(spec.function), name=spec.name, period_seconds=spec.period_seconds)


CRON_MAX_TRIES = 5  # arq attempts per cron/wake invocation when a job raises ``Retry`` (heavy-slot contention)


def build_cron(spec: CronSpec, gate: Callable[[Callable[..., Any]], Callable[..., Any]]) -> CronJob:
    """Build the arq CronJob around ``guarded_function`` with the spec's hashed fire times."""
    # max_tries > 1 lets a heavy-slot ``Retry`` (index_pending_chunks while ingestion holds the slot) re-run
    # within the tick instead of failing as "max 1 retries exceeded"; plain exceptions are never retried by arq.
    return _cron(guarded_function(spec, gate), run_at_startup=spec.run_at_startup, max_tries=CRON_MAX_TRIES,
                 **fire_times(spec))


def second_load(specs: Iterable[CronSpec]) -> dict[int, int]:
    """Histogram ``second-of-minute -> number of jobs that can fire in that second``.

    Used by tests and docs to prove the offsets keep every second lightly loaded.
    """
    load: dict[int, int] = {}
    for spec in specs:
        times = fire_times(spec)["second"]
        for second in ([times] if isinstance(times, int) else times):
            load[second] = load.get(second, 0) + 1
    return load
