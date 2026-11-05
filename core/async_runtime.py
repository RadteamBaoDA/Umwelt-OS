"""Event-loop executor policy: an explicit default thread pool and a separate small pool for DNS.

``asyncio.to_thread`` and ``run_in_executor(None, ...)`` share the loop's default executor, whose
implicit size is ``min(32, cpu + 4)``. Argon2, upload I/O, parsing and chunking all queue there, so
SSRF DNS lookups that outlive their ``wait_for`` timeout (a cancelled executor job keeps running
until the resolver returns) could starve them. DNS therefore runs on its own bounded pool, and the
default pool gets an explicit, documented size at API/worker startup.
"""

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

#: Dedicated resolver pool: bounds how many hung ``getaddrinfo`` calls can pile up (each is a blocked thread).
DNS_EXECUTOR_WORKERS = 8

_dns_executor: ThreadPoolExecutor | None = None
_dns_lock = Lock()


def default_executor_workers() -> int:
    """Return the default-executor size: ``THREAD_POOL_WORKERS`` if a positive integer, else CPython's ``min(32, cpu + 4)``.

    The fallback is the same value asyncio would pick implicitly; it is made explicit (not enlarged) so
    memory-heavy work such as 19 MiB argon2 hashes stays bounded on small hosts.
    """
    raw = os.getenv("THREAD_POOL_WORKERS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else min(32, (os.cpu_count() or 1) + 4)


def configure_default_executor() -> ThreadPoolExecutor:
    """Install an explicitly sized default executor on the running loop and return it.

    Must be called from a running event loop (API lifespan, arq worker startup). Idempotence is not
    required: each loop owns its executor and ``loop.close()`` shuts it down.
    """
    executor = ThreadPoolExecutor(max_workers=default_executor_workers(), thread_name_prefix="umwelt-default")
    asyncio.get_running_loop().set_default_executor(executor)
    return executor


def _get_dns_executor() -> ThreadPoolExecutor:
    """Return the process-wide lazily created resolver pool (thread-safe)."""
    global _dns_executor
    with _dns_lock:
        if _dns_executor is None:
            _dns_executor = ThreadPoolExecutor(max_workers=DNS_EXECUTOR_WORKERS, thread_name_prefix="umwelt-dns")
        return _dns_executor


async def run_dns_lookup[T](fn: Callable[..., T], /, *args: object) -> T:
    """Run a blocking resolver call (``socket.getaddrinfo``) on the dedicated DNS pool.

    Result and exceptions are identical to ``asyncio.to_thread(fn, *args)``. Callers keep their own
    ``wait_for`` deadline; a timed-out lookup then occupies only a DNS-pool thread, never the
    default executor shared with argon2, uploads and parsers.
    """
    return await asyncio.get_running_loop().run_in_executor(_get_dns_executor(), fn, *args)
