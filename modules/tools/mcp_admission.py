"""Shared bounded Redis leases for inbound and outbound MCP operations."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import Any, cast
from uuid import UUID

import anyio
from redis.asyncio import Redis
from redis.exceptions import RedisError

_NAMESPACE = "bbd:mcp:admission:"
_LEASE_TTL_MS = 90_000
_MAX_OPERATION_SECONDS = 60.0
_REDIS_PHASE_SECONDS = 1.0


def _redis_eval(redis: Redis, *args: Any) -> Awaitable[Any]:
    """Await-only view of Redis.eval; the asyncio client always returns an awaitable at runtime."""
    return cast("Awaitable[Any]", redis.eval(*args))

_CLAIM_OPERATION = """
if redis.call('exists', KEYS[3]) == 1 then return 0 end
if redis.call('exists', KEYS[1]) == 1 and redis.call('exists', KEYS[2]) == 1 then return 0 end
local slot = 1
if redis.call('exists', KEYS[1]) == 1 then slot = 2 end
redis.call('psetex', KEYS[slot], ARGV[2], ARGV[1])
redis.call('psetex', KEYS[3], ARGV[2], ARGV[1])
return slot
"""
_CLAIM_INBOUND = """
if redis.call('exists', KEYS[1]) == 1 and redis.call('exists', KEYS[2]) == 1 then return 0 end
local slot = 1
if redis.call('exists', KEYS[1]) == 1 then slot = 2 end
redis.call('psetex', KEYS[slot], ARGV[2], ARGV[1])
return slot
"""
_BIND_CLIENT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
local ttl = redis.call('pttl', KEYS[1])
if ttl <= 0 or redis.call('exists', KEYS[2]) == 1 then return 0 end
redis.call('psetex', KEYS[2], ttl, ARGV[1])
return 1
"""
_RELEASE = """
local released = 0
for _, key in ipairs(KEYS) do
    if redis.call('get', key) == ARGV[1] then
        released = released + redis.call('del', key)
    end
end
return released
"""
_CURRENT = """
for _, key in ipairs(KEYS) do
    if redis.call('get', key) ~= ARGV[1] or redis.call('pttl', key) <= 0 then return 0 end
end
return 1
"""

_operation_lease: ContextVar[tuple[object, tuple[str, ...], str] | None] = ContextVar(
    "mcp_operation_lease", default=None,
)


class McpAdmission:
    """Coordinate two shared MCP operation slots with token-qualified, expiring Redis leases."""

    def __init__(self, redis: Redis) -> None:
        """Keep the shared async Redis client and initialize admission as accepting work."""
        self.redis = redis
        self._stopped = False

    def stop(self) -> None:
        """Reject future lease claims while allowing current holders to finish or expire."""
        self._stopped = True

    async def _claim(
        self, script: str, keys: tuple[str, ...], token: str, deadline: float,
    ) -> int:
        """Run exactly one bounded atomic Redis admission script before the caller starts work."""
        remaining = deadline - asyncio.get_running_loop().time()
        if self._stopped or remaining <= 0:
            raise RuntimeError("MCP admission is unavailable")
        try:
            result = await asyncio.wait_for(
                _redis_eval(self.redis, script, len(keys), *keys, token, str(_LEASE_TTL_MS)),
                timeout=min(_REDIS_PHASE_SECONDS, remaining),
            )
        except (RedisError, TimeoutError) as exc:
            raise RuntimeError("MCP admission is unavailable") from exc
        return int(result)

    async def _release(self, keys: tuple[str, ...], token: str, deadline: float) -> None:
        """Delete only keys still owned by this random token, with one bounded Redis operation."""
        remaining = min(_REDIS_PHASE_SECONDS, deadline - asyncio.get_running_loop().time())
        if remaining <= 0 or not keys:
            return
        try:
            await asyncio.wait_for(
                _redis_eval(self.redis, _RELEASE, len(keys), *keys, token), timeout=remaining,
            )
        except (RedisError, TimeoutError):
            # The bounded TTL is the fallback when Redis cleanup is unavailable.
            return

    async def lease_current(self, inbound: "McpInboundLease | None" = None) -> bool:
        """Check an explicit inbound request lease or current outbound task lease and TTL."""
        if inbound is not None:
            if inbound.admission is not self:
                return False
            keys = (inbound.global_key, *((inbound.client_key,) if inbound.client_key else ()))
            token = inbound.token
        else:
            lease = _operation_lease.get()
            if lease is None or lease[0] is not self:
                return False
            _, keys, token = lease
        try:
            result = await asyncio.wait_for(
                _redis_eval(self.redis, _CURRENT, len(keys), *keys, token), timeout=_REDIS_PHASE_SECONDS,
            )
        except (RedisError, TimeoutError):
            return False
        return bool(result)

    @asynccontextmanager
    async def operation_slot(
        self, connection_id: UUID, deadline: float,
    ) -> AsyncIterator[None]:
        """Claim one global and one connection lease atomically, enforcing the bounded operation deadline."""
        token = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        started = loop.time()
        bounded_deadline = min(deadline, started + _MAX_OPERATION_SECONDS)
        if self._stopped or bounded_deadline <= started:
            raise RuntimeError("MCP admission is unavailable")
        connection_key = f"{_NAMESPACE}connection:{connection_id}"
        global_keys = (f"{_NAMESPACE}global:0", f"{_NAMESPACE}global:1")
        slot = await self._claim(
            _CLAIM_OPERATION, (*global_keys, connection_key), token, bounded_deadline,
        )
        if slot not in (1, 2):
            raise RuntimeError("MCP admission capacity is unavailable")
        keys = (global_keys[slot - 1], connection_key)
        context_token: Token[tuple[object, tuple[str, ...], str] | None] | None = None
        try:
            if loop.time() >= bounded_deadline:
                raise RuntimeError("MCP admission deadline expired")
            context_token = _operation_lease.set((self, keys, token))
            async with asyncio.timeout_at(bounded_deadline):
                yield
        finally:
            if context_token is not None:
                _operation_lease.reset(context_token)
            # Shield just the bounded compare-delete; an expired lease remains the cleanup fallback.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(_REDIS_PHASE_SECONDS):
                    await self._release(keys, token, loop.time() + _REDIS_PHASE_SECONDS)

    @asynccontextmanager
    async def inbound_slot(self, deadline: float) -> AsyncIterator["McpInboundLease"]:
        """Claim a global slot before authentication and yield its explicit request lease.

        Settled exits perform token-qualified cleanup within the original operation deadline
        and one-second Redis phase cap; a marked lease stays under its original 90-second TTL.
        """
        token = secrets.token_urlsafe(32)
        loop = asyncio.get_running_loop()
        started = loop.time()
        bounded_deadline = min(deadline, started + _MAX_OPERATION_SECONDS)
        if self._stopped or bounded_deadline <= started:
            raise RuntimeError("MCP admission is unavailable")
        global_keys = (f"{_NAMESPACE}global:0", f"{_NAMESPACE}global:1")
        slot = await self._claim(_CLAIM_INBOUND, global_keys, token, bounded_deadline)
        if slot not in (1, 2):
            raise RuntimeError("MCP admission capacity is unavailable")
        lease = McpInboundLease(self, global_keys[slot - 1], token, bounded_deadline)
        try:
            if loop.time() >= bounded_deadline:
                raise RuntimeError("MCP admission deadline expired")
            async with asyncio.timeout_at(bounded_deadline):
                yield lease
        finally:
            # Uncertain completion keeps this token's existing TTL; it never extends the lease.
            if not lease._retain_until_expiry:
                # The inbound lease is request state, not outbound task context.
                remaining = bounded_deadline - loop.time()
                if remaining > 0:
                    cleanup_deadline = min(
                        bounded_deadline,
                        loop.time() + _REDIS_PHASE_SECONDS,
                    )
                    with anyio.CancelScope(shield=True):
                        with anyio.move_on_after(cleanup_deadline - loop.time()):
                            keys = (lease.global_key, *((lease.client_key,) if lease.client_key else ()))
                            await self._release(keys, token, cleanup_deadline)


class McpInboundLease:
    """Explicit request-scoped holder that can bind one authenticated UUID to its global token."""

    def __init__(
        self, admission: McpAdmission, global_key: str, token: str, deadline: float,
    ) -> None:
        """Retain lease identity, deadline, optional client key and sticky cleanup uncertainty."""
        self.admission = admission
        self.global_key = global_key
        self.token = token
        self.deadline = deadline
        self.client_key: str | None = None
        self._retain_until_expiry = False

    def retain_until_expiry(self) -> None:
        """Retain this lease's current token keys until their existing Redis TTL expires.

        Call this when request completion or response-send outcome is uncertain. The flag is
        sticky for this lease and only suppresses context-exit compare-delete; it does not extend
        the 90-second TTL, renew ownership, release background work or physically stop a worker.
        Ordinary, settled request cleanup still uses the bounded token-qualified compare-delete.
        """
        self._retain_until_expiry = True

    async def bind_client(self, client_id: UUID) -> bool:
        """Atomically bind the held global token to one client key for the remaining lease TTL."""
        if self.client_key is not None:
            return False
        loop = asyncio.get_running_loop()
        remaining = self.deadline - loop.time()
        if self.admission._stopped or remaining <= 0:
            return False
        client_key = f"{_NAMESPACE}client:{client_id}"
        try:
            result = await asyncio.wait_for(
                _redis_eval(self.admission.redis, 
                    _BIND_CLIENT, 2, self.global_key, client_key, self.token,
                ), timeout=min(_REDIS_PHASE_SECONDS, remaining),
            )
        except (RedisError, TimeoutError):
            return False
        if not result:
            return False
        self.client_key = client_key
        return True
