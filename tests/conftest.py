"""Root pytest conftest: test environment defaults only."""

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("TEST_PUBLIC_ORIGIN", "http://localhost:3300")


# ---------------------------------------------------------------------------
# Event-loop blocking guardrail (remediation plan section 5A.2).
# Opt-in so the existing suite is unchanged: request ``loop_block_guard`` from an async test.
# ---------------------------------------------------------------------------
import asyncio
import gc
import logging
import re
import sys
from collections.abc import AsyncIterator

import pytest

SLOW_CALLBACK_SECONDS = 0.1
_SLOW_RE = re.compile(r"took (\d+(?:\.\d+)?) seconds")


class LoopBlockGuard(logging.Handler):
    """Collect asyncio debug-mode "Executing ... took N seconds" warnings above the 100 ms budget."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.slow: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Record slow-callback warnings emitted by ``asyncio`` debug mode (ignore every other message)."""
        message = record.getMessage()
        match = _SLOW_RE.search(message)
        if match and float(match.group(1)) >= SLOW_CALLBACK_SECONDS:
            self.slow.append(message)

    async def reset(self) -> None:
        """Yield once so the loop logs the current step, then forget it (call after building test inputs)."""
        gc.collect()  # a full collection after big allocations would otherwise be charged to the next callback
        await asyncio.sleep(0)
        self.slow.clear()

    def assert_clean(self) -> None:
        """Fail when any loop callback blocked the event loop for 100 ms or more."""
        assert not self.slow, "event loop blocked:\n" + "\n".join(self.slow)


@pytest.fixture
async def loop_block_guard() -> AsyncIterator[LoopBlockGuard]:
    """Run the test loop in asyncio debug mode with ``slow_callback_duration = 0.1`` and capture slow callbacks.

    The test (or the fixture teardown) fails through ``assert_clean`` when a callback held the loop
    for 100 ms or more; thread-offloaded work does not count because it runs outside the loop.
    """
    loop = asyncio.get_running_loop()
    previous = (loop.get_debug(), loop.slow_callback_duration)
    guard = LoopBlockGuard()
    logger = logging.getLogger("asyncio")
    logger.addHandler(guard)
    loop.set_debug(True)
    loop.slow_callback_duration = SLOW_CALLBACK_SECONDS
    # A CPU-bound worker thread holds the GIL for a full switch interval (5 ms) at a time; a shorter
    # interval keeps loop wake-ups from being charged for GIL waits on busy CI machines.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(0.001)
    try:
        yield guard
    finally:
        sys.setswitchinterval(previous_interval)
        loop.set_debug(previous[0])
        loop.slow_callback_duration = previous[1]
        logger.removeHandler(guard)
