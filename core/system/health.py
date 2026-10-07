import asyncio
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings

ARQ_WORKER_HEALTH_KEY = "bbd:worker:health"
ARQ_WORKER_GENERATION_KEY = "bbd:worker:generation"
PROBE_TIMEOUT_SECONDS = 1.0


async def system_health(
    session: AsyncSession, redis: Redis, settings: Settings
) -> dict[str, Any]:
    """Probe PostgreSQL, Redis, and worker heartbeat with bounded timeouts and report configured optional services without claiming connectivity."""
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), PROBE_TIMEOUT_SECONDS)
        postgres = "healthy"
    except (SQLAlchemyError, TimeoutError):
        postgres = "unavailable"

    redis_memory: dict[str, int] = {}
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await redis.ping()
            worker_heartbeat = await redis.get(ARQ_WORKER_HEALTH_KEY)
            try:
                memory = await redis.info("memory")
                redis_memory = {"used_bytes": int(memory.get("used_memory", 0)), "max_bytes": int(memory.get("maxmemory", 0))}
            except RedisError:
                pass  # INFO may be ACL-denied; memory is optional and must not mark Redis or the worker down
        redis_status = "healthy"
        worker_status = "healthy" if worker_heartbeat is not None else "unavailable"
    except (RedisError, TimeoutError):
        redis_status = "unavailable"
        worker_status = "unavailable"

    gateway_status = (
        "configured"
        if settings.omniroute_base_url and settings.omniroute_api_key.get_secret_value()
        else "unconfigured"
    )
    components = {
        "postgres": {"status": postgres},
        "redis": {"status": redis_status, **({"memory": redis_memory} if redis_memory else {})},
        "worker": {"status": worker_status},
        "model_gateway": {"status": gateway_status, "connectivity": "not_tested"},
        "graph": {"status": "not_installed"},
        "n8n": {"status": "not_installed"},
        "browser": {"status": "not_installed"},
    }
    core_statuses = (postgres, redis_status, worker_status)
    return {"overall": "healthy" if all(s == "healthy" for s in core_statuses) else "degraded",
            "components": components}
