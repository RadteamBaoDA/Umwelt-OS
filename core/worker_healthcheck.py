"""Container health probe for arq workers: a cheap Redis key check instead of ``arq --check``.

``arq --check`` imports the whole worker module graph on every probe. arq already refreshes a health key
with a TTL of ``health_check_interval + 1`` seconds, so key existence proves the worker loop is alive.
Usage: ``python -m core.worker_healthcheck <health key>``; exit code 0 healthy, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from core.config import Settings
from core.redis_client import create_api_redis


async def _alive(key: str) -> bool:
    """Return True when ``key`` exists; any Redis error or timeout counts as unhealthy."""
    redis = create_api_redis(Settings())
    try:
        return bool(await asyncio.wait_for(redis.exists(key), timeout=4))
    except Exception:  # noqa: BLE001 - the probe's only output is its exit code
        return False
    finally:
        with contextlib.suppress(Exception):  # closing must never change the verdict
            await redis.aclose()


def main(argv: list[str]) -> int:
    """Probe the health key named by ``argv[1]``; return the process exit code."""
    if len(argv) != 2 or not argv[1]:
        print("usage: python -m core.worker_healthcheck <health key>", file=sys.stderr)
        return 2
    return 0 if asyncio.run(_alive(argv[1])) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
