"""Horizontal-scale readiness: per-role DB timeouts, Redis Sentinel wiring, queue-full signalling, stateless workers."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from arq.connections import ArqRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

import apps.worker.main as worker_main
from core.config import Settings
from core.errors import install_error_handling
from core.redis_client import (
    build_redis_settings,
    build_worker_redis_pool,
    create_api_redis,
    parse_sentinels,
)
from core.system.health import queue_writable

ROOT = Path(__file__).resolve().parents[2]


def _settings(**env: Any) -> Settings:
    """Build hermetic development settings (no .env file) with overrides."""
    return Settings(_env_file=None, ENVIRONMENT="development", **env)  # type: ignore[call-arg]


class _FakeRedis:
    """Minimal async Redis double for worker startup."""

    async def set(self, *_args: object, **_kwargs: object) -> None:
        """Accept the worker-generation write."""


@pytest.mark.asyncio
async def test_worker_roles_pass_explicit_statement_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main worker, cron worker, model worker and chat worker all get a bounded, role-specific ``statement_timeout``."""
    seen: dict[str, dict[str, Any]] = {}
    role: list[str] = []

    class _Engine:
        """Stand-in engine with an awaitable dispose."""

        async def dispose(self) -> None:
            """No-op."""

    def fake_factory(_settings: Settings, **kwargs: Any) -> tuple[_Engine, object]:
        """Capture the role's engine arguments."""
        seen[role[-1]] = kwargs
        return _Engine(), object()

    async def fake_registry(*_args: object) -> tuple[object, object, object]:
        """Skip agent registry composition."""
        return object(), object(), object()

    monkeypatch.setattr(worker_main, "make_session_factory_for_role", fake_factory)
    monkeypatch.setattr(worker_main, "compose_agent_registry", fake_registry)
    monkeypatch.setattr(worker_main, "install_log_redaction", lambda: None)
    monkeypatch.setattr(worker_main, "set_process_role", lambda name: role.append(name))
    monkeypatch.setattr(
        worker_main,
        "Settings",
        lambda: _settings(
            DB_WORKER_STATEMENT_TIMEOUT_MS=111_000,
            DB_MODEL_WORKER_STATEMENT_TIMEOUT_MS=222_000,
        ),
    )

    async def no_catch_up(_ctx: object) -> list[str]:
        """Skip the cron worker's missed-run scan (needs a real Redis hash)."""
        return []

    monkeypatch.setattr(worker_main, "catch_up_missed_runs", no_catch_up)
    try:
        for hook in (
            worker_main.startup,
            worker_main.cron_startup,
            worker_main.model_startup,
            worker_main.chat_startup,
        ):
            await hook({"redis": _FakeRedis()})
    finally:
        worker_main.set_wake_hook(None)  # the full-ctx workers install a process-wide wake hook
    assert seen["worker"]["statement_timeout_ms"] == 111_000
    assert seen["cron-worker"]["statement_timeout_ms"] == 111_000
    assert seen["cron-worker"]["direct_pool_size"] == worker_main.CronWorkerSettings.max_jobs
    assert seen["model-worker"]["statement_timeout_ms"] == 222_000
    assert seen["chat-worker"]["statement_timeout_ms"] == 60_000
    assert all(
        kw["statement_timeout_ms"] > 0 for kw in seen.values()
    )  # nothing runs unbounded any more
    assert seen["worker"]["direct_pool_size"] == worker_main.WorkerSettings.max_jobs


def test_parse_sentinels() -> None:
    """Sentinel lists accept host or host:port, ignore blanks and reject malformed entries."""
    assert parse_sentinels(None) == [] and parse_sentinels(" , ") == []
    assert parse_sentinels("s1:26380, s2 ,s3:1") == [("s1", 26380), ("s2", 26379), ("s3", 1)]
    for bad in ("s1:abc", ":26379", "s1:0", "s1:70000"):
        with pytest.raises(ValueError):
            parse_sentinels(bad)


def test_worker_redis_settings_switch_to_sentinel() -> None:
    """Without sentinels the DSN is used unchanged; with them arq resolves the named master through Sentinel."""
    plain = build_redis_settings(_settings(REDIS_URL="redis://:pw@redis:6379/2"))
    assert (plain.host, plain.port, plain.database, plain.password, plain.sentinel) == (
        "redis",
        6379,
        2,
        "pw",
        False,
    )
    ha = build_redis_settings(
        _settings(
            REDIS_URL="redis://:pw@redis:6379/2",
            REDIS_SENTINELS="s1:26379,s2:26379",
            REDIS_MASTER_NAME="umwelt",
        )
    )
    assert ha.sentinel and ha.host == [("s1", 26379), ("s2", 26379)]
    assert ha.sentinel_master == "umwelt" and ha.password == "pw" and ha.database == 2


@pytest.mark.asyncio
async def test_api_redis_client_is_arq_redis_in_both_modes() -> None:
    """The API keeps the same ``ArqRedis`` type in single-instance and Sentinel mode (no I/O at creation)."""
    single = create_api_redis(_settings(REDIS_URL="redis://:pw@redis:6379/0"))
    ha = create_api_redis(
        _settings(REDIS_URL="redis://:pw@redis:6379/3", REDIS_SENTINELS="s1,s2:26380")
    )
    try:
        assert isinstance(single, ArqRedis) and isinstance(ha, ArqRedis)
        assert single.connection_pool.connection_kwargs["host"] == "redis"
        pool = ha.connection_pool
        assert getattr(pool, "service_name", None) == "mymaster"
        assert pool.connection_kwargs["db"] == 3 and pool.connection_kwargs["password"] == "pw"
    finally:
        await single.aclose()
        await ha.aclose()


@pytest.mark.parametrize(
    "error",
    [
        RedisConnectionError("down"),
        ResponseError("OOM command not allowed when used memory > 'maxmemory'"),
    ],
)
def test_unhandled_redis_failure_is_a_retryable_503(error: Exception) -> None:
    """Enqueue failures (Redis down, or full under noeviction) answer 503 + Retry-After, never a bare 500."""
    app = FastAPI()
    install_error_handling(app)

    @app.get("/enqueue")
    async def enqueue() -> None:
        """Raise the parametrized Redis failure like a failed ``enqueue_job``."""
        raise error

    response = TestClient(app, raise_server_exceptions=False).get("/enqueue")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    assert "OOM" not in response.text and "maxmemory" not in response.text


def test_queue_writable_signal() -> None:
    """The health signal flips before the noeviction cap is reached and stays unknown when uncapped."""
    assert queue_writable({}) is None
    assert queue_writable({"used_bytes": 10, "max_bytes": 0}) is None
    assert queue_writable({"used_bytes": 90, "max_bytes": 100}) is True
    assert queue_writable({"used_bytes": 96, "max_bytes": 100}) is False


# --- statelessness audit -------------------------------------------------------------------------------
# Every module-level asyncio.Lock/Semaphore/Event or in-process cache must be classified here. Adding one
# without deciding how it behaves with N replicas fails this test.
PER_PROCESS_STATE_ALLOWLIST = {
    # Browser sidecar is a single-instance singleton by design (service_instance_id fence in every request).
    "modules/connectors/crawl.py": {"_job_lock", "_agent_read_tasks"},
    # In-process wakeup hint only; each SSE stream also polls PostgreSQL every 2 s, so a missed hint on
    # another replica adds latency, never loses an event.
    "core/realtime/routes.py": {"_WORKSPACE_WAKEUPS"},
    # 60 s TTL, workspace/authority/generation scoped key: a stale entry on one replica is bounded and safe.
    "modules/search/public.py": {"_QUERY_VECTOR_CACHE"},
    # Process-wide lazily built validator cache (pure function of the schema text).
    "core/tools/validator.py": {"_VALIDATOR_CACHE"},
    # One-hour CoinGecko id catalog refreshed under a per-process lock; every replica fetches its own copy.
    "modules/connectors/coin_catalog.py": {"_lock"},
    # Live-map response/tile caches and burst pacing are courtesy limits per API process (already N=WEB_CONCURRENCY
    # processes today); the durable provider budget and retry deadline live in PostgreSQL (live_map.py) and stay
    # authoritative, so extra replicas only multiply the soft burst allowance, never the real quota.
    "modules/dashboard/live_map_routes.py": {
        "_cache",
        "_tiles",
        "_next_send",
        "_recent",
        "_lock",
        "_parallel",
    },
    # Per-process weather TTL cache (bounded to _MAX_ENTRIES); a miss costs one provider call.
    "modules/dashboard/local_weather.py": {"_CACHE"},
    # Static module registry memoised on first use (immutable after build).
    "modules/automations/_execution/types.py": {"_modules_cache"},
}
STATE_CALLS = {"Lock", "Semaphore", "Event", "BoundedSemaphore"}


def _module_level_state(tree: ast.Module) -> set[str]:
    """Names of module-level asyncio primitives and mutable cache containers named like caches/locks/cursors."""
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        func = value.func if isinstance(value, ast.Call) else None
        is_primitive = (
            isinstance(func, ast.Attribute)
            and func.attr in STATE_CALLS
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            lowered = target.id.lower()
            is_cache = isinstance(value, ast.Dict | ast.List | ast.Set) and any(
                word in lowered
                for word in ("cache", "wakeup", "tasks", "tiles", "next_send", "recent", "inflight")
            )
            if is_primitive or is_cache or isinstance(value, ast.Dict) and "cache" in lowered:
                names.add(target.id)
    return names


def test_per_process_state_is_classified() -> None:
    """Module-level locks/caches are exactly the audited, documented per-process set (multi-replica safe)."""
    found: dict[str, set[str]] = {}
    for top in ("apps", "core", "modules"):
        for path in (ROOT / top).rglob("*.py"):
            names = _module_level_state(ast.parse(path.read_text(encoding="utf-8")))
            if names:
                found[path.relative_to(ROOT).as_posix()] = names
    assert found == PER_PROCESS_STATE_ALLOWLIST


def test_worker_cross_job_coordination_is_not_a_process_lock_for_correctness() -> None:
    """The only worker-ctx ``asyncio.Lock`` serialises an in-process scan cursor; claims stay row-fenced in SQL."""
    source = (ROOT / "core/events/dispatch.py").read_text(encoding="utf-8")
    assert "with_for_update" in source or "FOR UPDATE" in source or "skip_locked" in source
    assert "SAFE PER PROCESS" in (ROOT / "apps/worker/main.py").read_text(encoding="utf-8")


def test_pgbouncer_overlay_routes_every_database_client_through_the_pooler() -> None:
    """Each DB-using service gets a pooled URL, the direct URL and PGBOUNCER_ENABLED; migrate stays direct."""
    import yaml

    overlay = yaml.safe_load((ROOT / "docker-compose.pgbouncer.yml").read_text(encoding="utf-8"))
    services = overlay["services"]
    image = services["pgbouncer"]["image"]
    assert ":" in image.split("/")[-1] and not image.endswith(":latest")  # pinned tag
    command = services["pgbouncer"]["command"][0]
    assert "pool_mode = transaction" in command
    aliases = {
        "api": "bbd_api",
        "worker": "bbd_worker",
        "chat-worker": "bbd_chat",
        "model-worker": "bbd_model",
    }
    for name, alias in aliases.items():
        env = services[name]["environment"]
        assert env["PGBOUNCER_ENABLED"] == "true"
        assert f"@pgbouncer:6432/{alias}" in env["DATABASE_URL"]
        assert "@postgres:5432/" in env["DATABASE_DIRECT_URL"]
        assert f"{alias} = " in command  # the alias exists in the rendered pgbouncer.ini
    assert "@postgres:5432/" in services["migrate"]["environment"]["DATABASE_DIRECT_URL"]
    assert "DATABASE_URL" not in services["migrate"]["environment"]


@pytest.mark.asyncio
async def test_worker_sentinel_pool_carries_sentinel_password_and_api_timeouts() -> None:
    """Worker Sentinel mode builds its own pool so the sentinel password and socket timeouts match the API."""
    assert build_worker_redis_pool(_settings(REDIS_URL="redis://:pw@redis:6379/2")) is None
    env = {
        "REDIS_URL": "redis://:pw@redis:6379/2",
        "REDIS_SENTINELS": "s1:26379,s2",
        "REDIS_SENTINEL_PASSWORD": "sp",
    }
    worker = build_worker_redis_pool(_settings(**env))
    api = create_api_redis(_settings(**env))
    try:
        assert isinstance(worker, ArqRedis) and worker is not None
        for client in (worker, api):
            pool = client.connection_pool
            assert pool.sentinel_manager.sentinel_kwargs["password"] == "sp"
            assert pool.connection_kwargs["password"] == "pw" and pool.connection_kwargs["db"] == 2
            assert (
                pool.connection_kwargs["socket_timeout"] == 5
                and pool.connection_kwargs["retry_on_timeout"]
            )
        assert not worker.connection_pool.connection_kwargs.get(
            "decode_responses"
        )  # arq decodes bytes itself
    finally:
        await worker.aclose() if worker else None
        await api.aclose()


def test_worker_settings_expose_sentinel_pool_attribute() -> None:
    """Every ARQ settings class declares ``redis_pool`` (None in direct mode) so Sentinel mode can supply it."""
    for cls in (
        worker_main.WorkerSettings,
        worker_main.ModelWorkerSettings,
        worker_main.ChatWorkerSettings,
    ):
        assert hasattr(cls, "redis_pool")
