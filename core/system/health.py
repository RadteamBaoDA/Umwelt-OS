import asyncio
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings

ARQ_WORKER_HEALTH_KEY = "bbd:worker:health"
ARQ_CHAT_WORKER_HEALTH_KEY = "arq:chat:health-check"
ARQ_MODEL_WORKER_HEALTH_KEY = "arq:model:health-check"
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
            chat_heartbeat = await redis.get(ARQ_CHAT_WORKER_HEALTH_KEY)
            model_heartbeat = await redis.get(ARQ_MODEL_WORKER_HEALTH_KEY)
            try:
                memory = await redis.info("memory")
                redis_memory = {"used_bytes": int(memory.get("used_memory", 0)), "max_bytes": int(memory.get("maxmemory", 0))}
            except RedisError:
                pass  # INFO may be ACL-denied; memory is optional and must not mark Redis or the worker down
        redis_status = "healthy"
        worker_status = "healthy" if worker_heartbeat is not None else "unavailable"
        chat_worker_status = "healthy" if chat_heartbeat is not None else "unavailable"
        model_worker_status = "healthy" if model_heartbeat is not None else "unavailable"
    except (RedisError, TimeoutError):
        redis_status = "unavailable"
        worker_status = "unavailable"
        chat_worker_status = "unavailable"
        model_worker_status = "unavailable"

    # Configured = some owner saved a provider with an endpoint (no env bootstrap); not a connectivity claim.
    gateway_configured = n8n_stored = False
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            if postgres != "healthy":
                raise TimeoutError
            gateway_configured = bool((await session.execute(text(
                "SELECT 1 FROM ai_settings, jsonb_each(providers) AS p(id, rec) "
                "WHERE rec->>'base_url' IS NOT NULL LIMIT 1"))).first())
            n8n_stored = bool((await session.execute(text(
                "SELECT 1 FROM ai_settings WHERE n8n->>'api_key_ciphertext' IS NOT NULL LIMIT 1"))).first())
    except (SQLAlchemyError, TimeoutError):
        pass
    gateway_status = "configured" if gateway_configured else "unconfigured"
    components = {
        "postgres": {"status": postgres},
        "redis": {"status": redis_status, **({"memory": redis_memory} if redis_memory else {})},
        "worker": {"status": worker_status},
        "chat_worker": {"status": chat_worker_status},
        "model_worker": {"status": model_worker_status},
        "model_gateway": {"status": gateway_status, "connectivity": "not_tested"},
        "graph": {"status": "not_installed"},
        # Optional sidecars: never part of `overall`; configured means a key/token is set, not reachable.
        "n8n": {"status": "configured" if n8n_stored or settings.n8n_api_key.get_secret_value() else "not_configured", "optional": True},
        "browser": {"status": "configured" if settings.browser_shared_token.get_secret_value() else "not_configured", "optional": True},
    }
    core_statuses = (postgres, redis_status, worker_status, chat_worker_status, model_worker_status)
    return {"overall": "healthy" if all(s == "healthy" for s in core_statuses) else "degraded",
            "components": components}
