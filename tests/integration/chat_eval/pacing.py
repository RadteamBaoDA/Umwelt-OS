"""Start pacing for the eval's rerank calls, for a provider trial key with a per-minute limit.

``CHAT_EVAL_RERANK_RPM`` caps how many hybrid cases may start in any rolling 60 s window. Each hybrid case
triggers at most one rerank call, so pacing its start paces the rerank calls. The limiter is shared by every
concurrent case of one run and uses only asyncio, so an eval run never sleeps in a thread.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

WINDOW_S = 60.0
RPM_ENV = "CHAT_EVAL_RERANK_RPM"
TPM_ENV = "CHAT_EVAL_EMBED_TPM"


def rerank_rpm(env: Mapping[str, str]) -> int | None:
    """The starts-per-minute limit from ``CHAT_EVAL_RERANK_RPM``, or None for unlimited (pure).

    Blank, unset or ``0`` means unlimited. A non-integer or negative value raises ``ValueError`` so a typo
    fails the run before any model call instead of silently running unpaced.
    """
    raw = env.get(RPM_ENV, "").strip()
    if not raw:
        return None
    value = int(raw)  # ValueError on a non-integer, reported as is
    if value < 0:
        raise ValueError(f"{RPM_ENV} must not be negative")
    return value or None


class StartPacer:
    """Grants at most ``rpm`` starts in any rolling ``WINDOW_S`` window; later callers wait.

    ``clock`` and ``sleep`` are seams for tests (a fake clock avoids real waits). Check and record happen with
    no ``await`` between them, so concurrent callers on one event loop cannot both take the last slot. A waiter
    re-checks after each sleep because another caller may have taken the slot it was waiting for.
    """

    def __init__(
        self,
        rpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        """Create a pacer for ``rpm`` starts per window; ``rpm`` must be at least 1."""
        if rpm < 1:
            raise ValueError("rpm must be at least 1")
        self._rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()

    async def acquire(self) -> None:
        """Wait until a start fits in the window, then record it (async; no return value).

        Starts older than ``WINDOW_S`` are dropped first. When the window is full, sleeps until the oldest
        recorded start leaves it, then re-checks.
        """
        while True:
            now = self._clock()
            while self._starts and now - self._starts[0] >= WINDOW_S:
                self._starts.popleft()
            if len(self._starts) < self._rpm:
                self._starts.append(now)
                return
            await self._sleep(self._starts[0] + WINDOW_S - now)


def embed_tpm(env: Mapping[str, str]) -> int | None:
    """The embedding tokens-per-minute budget from ``CHAT_EVAL_EMBED_TPM``, or None for unpaced (pure).

    Blank, unset or ``0`` means unpaced. A non-integer or negative value raises ``ValueError`` so a typo fails
    the run before indexing.
    """
    raw = env.get(TPM_ENV, "").strip()
    if not raw:
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"{TPM_ENV} must not be negative")
    return value or None


def estimate_tokens(inputs: list[str]) -> int:
    """Rough token count of an embedding batch: ``len(text) // 3`` per input, at least 1 each (pure).

    Errs high for English so the pacer stays under the provider limit.
    """
    return sum(max(1, len(text) // 3) for text in inputs)


class TokenPacer:
    """Keeps the estimated tokens sent in any rolling ``WINDOW_S`` window at or under ``budget``.

    Same seams as ``StartPacer``. A single request larger than the whole budget is let through once the window
    is empty, so an oversized batch cannot wait forever. Check and record have no ``await`` between them.
    """

    def __init__(
        self,
        budget: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        """Create a pacer for ``budget`` tokens per window; ``budget`` must be at least 1."""
        if budget < 1:
            raise ValueError("budget must be at least 1")
        self._budget = budget
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[tuple[float, int]] = deque()
        self._total = 0

    async def acquire(self, tokens: int) -> None:
        """Wait until ``tokens`` fit in the window, then record them (async; no return value).

        Entries older than ``WINDOW_S`` are dropped first; when the request does not fit, sleeps until the
        oldest entry leaves the window and re-checks.
        """
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0][0] >= WINDOW_S:
                self._total -= self._sent.popleft()[1]
            if not self._sent or self._total + tokens <= self._budget:
                self._sent.append((now, tokens))
                self._total += tokens
                return
            await self._sleep(self._sent[0][0] + WINDOW_S - now)
