"""Exponential backoff with jitter for arq job retries.

Every job that used a fixed ``Retry(defer=N)`` now computes its delay here so retries of many jobs that
failed together (a Redis blip, a provider outage, a busy heavy slot) spread out instead of returning in
lock-step. The delay for try ``n`` (1-based, from ``ctx["job_try"]``) is the "equal jitter" form::

    ceiling = min(cap, base * 2 ** (n - 1))
    delay   = ceiling / 2 + uniform(0, ceiling / 2)

so it is never below half the ceiling (no hot loops), never above ``cap`` and grows until the cap. Caps
are per call site and documented in ``docs/operations.md``.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from typing import Any

from arq.worker import Retry

MAX_EXPONENT = 20  # 2**20 is far beyond any cap; bounds the shift for absurd try counts


def current_try(ctx: Mapping[str, Any] | None) -> int:
    """Return the 1-based arq attempt number from a job ctx (1 when absent or malformed)."""
    value = (ctx or {}).get("job_try")
    return value if isinstance(value, int) and value >= 1 else 1


def backoff_delay(
    job_try: int, *, base: float, cap: float, rand: Callable[[], float] = random.random,
) -> float:
    """Return the jittered exponential delay in seconds for attempt ``job_try`` (see module docstring).

    ``base`` and ``cap`` must be positive with ``base <= cap``. ``rand`` returns a float in ``[0, 1)``
    and is injectable for deterministic tests.
    """
    if base <= 0 or cap < base:
        raise ValueError("require 0 < base <= cap")
    exponent = min(max(job_try, 1) - 1, MAX_EXPONENT)
    ceiling = min(cap, base * (2 ** exponent))
    return float(ceiling / 2 + rand() * (ceiling / 2))


def retry_with_backoff(ctx: Mapping[str, Any] | None, *, base: float, cap: float) -> Retry:
    """Build the arq ``Retry`` for the current attempt; callers ``raise retry_with_backoff(...) from exc``."""
    return Retry(defer=backoff_delay(current_try(ctx), base=base, cap=cap))
