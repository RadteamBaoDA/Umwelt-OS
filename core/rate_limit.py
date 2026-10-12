"""Per-session request budgets (OWASP API4) for routes that spend LLM, embedding or crawl capacity."""

import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from redis.exceptions import RedisError


def _identity(request: Request) -> str:
    """Return an opaque hash of the session cookie, bearer token, or client address, in that order."""
    from core.auth.dependencies import SESSION_COOKIE as cookie_name

    raw = (
        request.cookies.get(cookie_name)
        or request.headers.get("authorization")
        or (request.client.host if request.client else "unknown")
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def require_budget(name: str, limit: int, window: int) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency allowing `limit` requests per `window` seconds per session for `name`.

    Fixed-window counter in Redis, keyed by hashed session so accounts never share budget.
    Exceeding it raises 429 with Retry-After in the project error envelope. A Redis outage
    raises 503 (fail-closed) unless settings.request_budget_fail_open is true. A settings
    `request_budget_overrides` entry `{name: [limit, window]}` replaces the defaults.
    """

    async def dependency(request: Request) -> None:
        """Count this request against the caller's budget and reject it when exhausted."""
        settings: Any = request.app.state.settings
        if not getattr(settings, "request_budget_enabled", True):
            return
        lim, win = getattr(settings, "request_budget_overrides", {}).get(name, (limit, window))
        now = int(time.time())
        key = f"budget:{name}:{_identity(request)}:{now // win}"
        try:
            pipeline = request.app.state.redis.pipeline(transaction=True)
            pipeline.incr(key)
            pipeline.expire(key, win * 2, nx=True)
            count = (await pipeline.execute())[0]
        except (RedisError, OSError, TimeoutError) as exc:
            if getattr(settings, "request_budget_fail_open", False):
                return
            raise HTTPException(
                status_code=503,
                detail={"message": "Request budget is temporarily unavailable", "code": "TEMPORARILY_UNAVAILABLE"},
            ) from exc
        if count > lim:
            raise HTTPException(
                status_code=429,
                detail={"message": "Request budget exceeded", "code": "RATE_LIMITED", "details": {"budget": name}},
                headers={"Retry-After": str(win - now % win)},
            )

    return dependency
