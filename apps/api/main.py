import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, cast

from arq.connections import ArqRedis
from arq.constants import default_queue_name
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from core.api_headers import ApiDefaultHeadersMiddleware
from core.async_runtime import configure_default_executor
from core.auth.dependencies import session_middleware_secret
from core.body_limit import BodyLimitMiddleware
from core.config import Settings
from core.cron import CRON_QUEUE, set_wake_hook, wake
from core.database import dispose_engine, make_session_factory_for_role
from core.errors import install_error_handling
from core.events.registry import validate_event_contracts
from core.metrics import monitor_event_loop_lag, run_sampler_loop, spawn_background, stop_background
from core.metrics_http import MetricsMiddleware, metrics_enabled
from core.model_gateway.pool import close_pinned_clients
from core.modules import ModuleRegistry, ordered_router_specs, register_modules
from core.publication import PublicationGateMiddleware
from core.realtime.routes import MAX_STREAMS_PER_API_PROCESS
from core.redis_client import create_api_redis
from core.telemetry import install_log_redaction
from core.tools import ToolRegistry
from core.tracing import TracingArqRedis, configure_tracing, instrument_engine, instrument_fastapi
from core.worker_queues import MODEL_QUEUE
from modules.agents.handoff import register_handoff_tool
from modules.automations.tools import register_automation_tools
from modules.chat.routes import MAX_CHAT_STREAMS_PER_API_PROCESS
from modules.chat.worker import CHAT_QUEUE
from modules.dashboard.live_map_routes import LiveMapHeadersMiddleware
from modules.goals.tools import register_goal_tools
from modules.mcp_widgets.server import RootPath
from modules.mcp_widgets.server import create_http_app as create_widgets_mcp_app
from modules.observability.metrics_sampler import build_api_samplers
from modules.tasks.tools import register_task_tools
from modules.tools.browser import register_browser_tool
from modules.tools.builtins import register_builtin_tools
from modules.tools.public import McpAdmission, McpRuntime, create_inbound_mcp_bundle
from modules.tools.webhook import register_webhook_tool


def _validated_csrf_secret(app_settings: Settings) -> str:
    """Return the configured CSRF signing secret after API-only startup validation.

    Raises:
        RuntimeError: If a worker-count variable is not a positive integer, or several API
            workers would each sign with a different generated secret.
    """
    app_settings.require_trusted_client_ip_source()  # API-only: workers never key on client IP
    csrf_secret = app_settings.csrf_signing_secret.get_secret_value()
    worker_counts = []
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):  # uvicorn honours both (click auto_envvar_prefix)
        workers_raw = os.getenv(name, "1").strip() or "1"
        if not workers_raw.isdigit():
            raise RuntimeError(f"{name} must be a positive integer, got {workers_raw!r}")
        worker_counts.append(int(workers_raw))
    if not csrf_secret and max(worker_counts) > 1:
        raise RuntimeError("CSRF_SIGNING_SECRET is required when WEB_CONCURRENCY or UVICORN_WORKERS > 1 (each worker would sign with a different secret)")
    return csrf_secret


def _make_lifespan(redis: ArqRedis, engine: AsyncEngine) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the lifespan context that runs the SDK managers and always closes shared clients.

    Admission is stopped before the SDK manager drains; Redis and SQLAlchemy are closed even
    when manager entry or sequential owner hydration fails during startup.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the SDK manager once, hydrate bounded owner state, and always close shared clients.

        Admission is stopped before the SDK manager drains; Redis and SQLAlchemy are closed even
        when manager entry or sequential owner hydration fails during startup. The loop's default
        thread executor is given an explicit size first (see ``core.async_runtime``).
        """
        configure_default_executor()
        metrics_tasks = _start_metrics_tasks(_app)

        async def wake_poller(job_name: str, defer_seconds: float = 1.0) -> None:
            """Enqueue an immediate run of a worker poller after an API commit (best-effort)."""
            await wake(redis, job_name, defer_seconds=defer_seconds)

        set_wake_hook(wake_poller)  # lets outbox publishers wake the dispatcher; cleared on exit
        try:
            async with AsyncExitStack() as stack:
                try:
                    widgets = getattr(_app.state, "mcp_widgets_server", None)
                    if widgets is not None:
                        await stack.enter_async_context(widgets.session_manager.run())
                    bundle = getattr(_app.state, "mcp_bundle", None)
                    if bundle is not None:
                        await stack.enter_async_context(bundle.server.session_manager.run())
                        await _app.state.mcp_runtime.hydrate_connections()
                    yield
                finally:
                    admission = getattr(_app.state, "mcp_admission", None)
                    if admission is not None:
                        admission.stop()
        finally:
            set_wake_hook(None)  # no more wake-ups once the Redis client is about to close
            await stop_background(metrics_tasks)
            try:
                await close_pinned_clients()  # pooled model-gateway HTTP clients
                await redis.aclose()
            finally:
                await dispose_engine(engine)

    return lifespan


def _start_metrics_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    """Start the event-loop-lag monitor and the shared-state sampler loop when ``/metrics`` can be served.

    Returns the tasks so the lifespan can cancel them before closing Redis and the engine. Nothing
    runs (no Redis or database traffic) when metrics are disabled for this deployment.
    """
    app_settings: Settings = app.state.settings
    if not metrics_enabled(app_settings):
        return []
    samplers = build_api_samplers(app.state.redis, app.state.session_factory, (default_queue_name, MODEL_QUEUE, CHAT_QUEUE, CRON_QUEUE),
    )
    return [
        spawn_background(monitor_event_loop_lag(), "metrics-loop-lag"),
        spawn_background(run_sampler_loop(samplers), "metrics-samplers"),
    ]


def _new_app(
    app_settings: Settings, csrf_secret: str, session_factory: async_sessionmaker[AsyncSession], redis: ArqRedis,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
) -> FastAPI:
    """Create the FastAPI instance with app state and middleware in the historical order.

    Middleware registration order is significant (later registrations wrap earlier ones), so it
    is kept exactly: session, log redaction + error handling, publication gate, live-map
    headers, then body limit.
    """
    # Hardening: interactive API docs expose the full schema; serve them only in dev/test.
    app = FastAPI(
        title="Umwelt-OS",
        lifespan=lifespan,
        docs_url="/docs" if app_settings.is_dev else None,
        redoc_url="/redoc" if app_settings.is_dev else None,
        openapi_url="/openapi.json" if app_settings.is_dev else None,
    )
    app.state.settings = app_settings
    app.state.session_factory = session_factory
    app.state.realtime_connections = asyncio.Semaphore(MAX_STREAMS_PER_API_PROCESS)
    app.state.chat_streams = asyncio.Semaphore(MAX_CHAT_STREAMS_PER_API_PROCESS)
    app.state.redis = redis
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_middleware_secret(csrf_secret),
        https_only=app_settings.secure_cookies,
        same_site="lax",
    )
    install_log_redaction()
    install_error_handling(app)
    app.add_middleware(PublicationGateMiddleware)
    app.add_middleware(LiveMapHeadersMiddleware)
    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=app_settings.max_request_body_bytes,
        upload_limit=app_settings.upload_max_bytes + 1024 * 1024,
    )
    app.add_middleware(ApiDefaultHeadersMiddleware)
    # Outermost: times every request (including rejections by inner middleware) and serves /metrics.
    app.add_middleware(MetricsMiddleware, settings=app_settings)
    return app


def _include_descriptor_routers(app: FastAPI, registry: ModuleRegistry) -> None:
    """Mount every core and descriptor-declared router in ascending ``RouterSpec.order``.

    Order is first-match-wins for route resolution and is guarded by the route-table baseline
    test. Routers flagged ``when_enabled`` are skipped when their owning descriptor is disabled
    (today only the tools management API); all others mount regardless, with per-route module
    gates enforcing availability.
    """
    for spec, owner in ordered_router_specs(registry):
        if spec.when_enabled and (owner is None or not owner.enabled):
            continue
        app.include_router(spec.load())


def _register_native_tools(tool_registry: ToolRegistry, app_settings: Settings, declared_tools: frozenset[str]) -> None:
    """Register the native tool handlers contributed by enabled module descriptors."""
    register_builtin_tools(tool_registry, declared_tools)
    register_webhook_tool(tool_registry, app_settings)
    register_browser_tool(tool_registry)
    register_handoff_tool(tool_registry, declared_tools)
    register_task_tools(tool_registry, declared_tools)
    register_goal_tools(tool_registry, declared_tools)
    register_automation_tools(tool_registry, declared_tools)


def _compose_tools_module(app: FastAPI, app_settings: Settings, redis: ArqRedis) -> None:
    """Create the shared MCP admission/runtime and mount the guarded inbound server.

    Owner routes (mounted earlier by order) precede the exact slash-terminated inbound mount;
    the lifespan enters one root-managed SDK session manager and drains admission on exit.
    """
    tool_registry = app.state.tool_registry
    admission = McpAdmission(redis)
    runtime = McpRuntime(
        tool_registry,
        app.state.session_factory,
        redis,
        app_settings,
        admission,
        approved_destination_cidrs=app_settings.mcp_allowed_endpoint_cidrs,
    )
    audience, authority, origin = runtime.audience, runtime.authority, runtime.origin
    bundle = create_inbound_mcp_bundle(
        registry=tool_registry,
        session_factory=app.state.session_factory,
        redis=redis,
        settings=app_settings,
        admission=admission,
        audience=audience,
        expected_host=authority,
        allowed_origins=frozenset({origin}),
        authenticate_inbound=runtime.authenticate_inbound,
        authorize_inbound=runtime.authorize_inbound,
        revalidate_inbound=runtime.revalidate_inbound,
        revalidate_inbound_output=runtime.revalidate_inbound_output,
    )
    app.state.mcp_admission = admission
    app.state.mcp_runtime = runtime
    app.state.mcp_bundle = bundle
    app.mount("/api/v1/mcp", bundle.guarded_asgi_app)


def _mount_widgets_server(app: FastAPI, app_settings: Settings) -> None:
    """Mount the optional widget MCP server when enabled.

    Raises when the bearer token is unset, so a misconfigured deployment fails at startup.
    """
    widgets_server, widgets_app = create_widgets_mcp_app(app_settings)
    app.state.mcp_widgets_server = widgets_server
    # Exact `/mcp` first so clients need no trailing slash (a Mount would 307-redirect it).
    app.router.routes.append(Route("/mcp", cast(Any, RootPath(widgets_app)), include_in_schema=False))
    app.mount("/mcp", widgets_app)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose account/workspace identity routes, owner security and domain/tool services.

    Args:
        settings: Optional settings override; absent uses validated process configuration.
    Returns:
        FastAPI application with lifespan-owned database/Redis clients and app-scoped modules.
    Side effects:
        Creates clients and registers only tool names contributed by enabled descriptors. Routers
        come from module descriptors in their declared global order. When tools is enabled, it
        composes shared admission/runtime callbacks, owner routes before the exact
        slash-terminated inbound mount, and one root-managed SDK session manager. Lifespan
        hydrates at most the runtime's bounded owner catalog and drains admission before manager
        exit; Redis and SQLAlchemy disposal run even when startup hydration fails.
    """
    app_settings = settings or Settings()
    csrf_secret = _validated_csrf_secret(app_settings)
    engine, session_factory = make_session_factory_for_role(
        app_settings,
        pool_size=app_settings.db_pool_size,
        max_overflow=app_settings.db_max_overflow,
        statement_timeout_ms=app_settings.db_statement_timeout_ms,
    )
    tracing_on = configure_tracing(
        app_settings.otel_exporter_otlp_endpoint, service_name="umwelt-api",
        sample_ratio=app_settings.otel_traces_sample_ratio,
    )
    if tracing_on:
        instrument_engine(engine)
    # ArqRedis subclasses Redis, so existing callers work and enqueue_job exists for chat/agent dispatch.
    # TracingArqRedis adds the caller's trace context to job kwargs only while tracing is on; the factory keeps
    # the Sentinel-aware connection setup (direct or Sentinel-resolved master).
    redis = create_api_redis(app_settings, redis_class=TracingArqRedis)
    app = _new_app(app_settings, csrf_secret, session_factory, redis, _make_lifespan(redis, engine))
    instrument_fastapi(app)  # no-op unless tracing is on
    app.state.modules = register_modules()
    validate_event_contracts()  # fail fast on undeclared outbox event types / handlers
    _include_descriptor_routers(app, app.state.modules)
    tool_registry = ToolRegistry(module_registry=app.state.modules)
    declared_tools = frozenset(name for item in app.state.modules.values() if item.enabled for name in item.tools)
    if app.state.modules["tools"].enabled and declared_tools:
        _register_native_tools(tool_registry, app_settings, declared_tools)
    app.state.tool_registry = tool_registry
    if app.state.modules["tools"].enabled:
        _compose_tools_module(app, app_settings, redis)
    if app_settings.mcp_widgets_enabled:
        _mount_widgets_server(app, app_settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return the process liveness response."""
        return {"status": "ok"}

    return app
