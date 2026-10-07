import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from arq.connections import ArqRedis
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from core.auth.routes import router as auth_router
from core.body_limit import BodyLimitMiddleware
from core.config import Settings
from core.database import make_session_factory
from core.errors import install_error_handling
from core.modules import register_modules
from core.realtime_routes import MAX_STREAMS_PER_API_PROCESS
from core.realtime_routes import router as realtime_router
from core.system.routes import router as system_router
from core.telemetry import install_log_redaction
from core.tools import ToolRegistry
from modules.agents.handoff import register_handoff_tool
from modules.agents.routes import router as agents_router
from modules.automations.routes import router as automations_router
from modules.automations.routes import webhook_router as automation_webhook_router
from modules.automations.tools import register_automation_tools
from modules.backup.middleware import BackupActivityMiddleware
from modules.backup.routes import router as backup_router
from modules.chat.routes import router as chat_router
from modules.connectors.github.routes import router as github_oauth_router
from modules.connectors.provisioning_routes import router as connector_provisioning_router
from modules.connectors.routes import router as connectors_router
from modules.dashboard.routes import router as dashboard_router
from modules.export.routes import router as export_router
from modules.goals.routes import router as goals_router
from modules.goals.tools import register_goal_tools
from modules.ingestion.routes import documents_router as document_upload_router
from modules.ingestion.routes import router as ingestion_router
from modules.knowledge.documents.routes import router as documents_router
from modules.knowledge.entities.routes import router as entities_router
from modules.knowledge.observations.routes import router as observations_router
from modules.knowledge.relationships.routes import router as relationships_router
from modules.knowledge.temporal.routes import router as temporal_router
from modules.memory.routes import router as memory_router
from modules.model_gateway.routes import router as model_gateway_router
from modules.news.routes import router as news_router
from modules.news.topics import router as topics_router
from modules.notifications.routes import router as notifications_router
from modules.observability.operations_routes import router as operations_router
from modules.observability.routes import router as observability_router
from modules.search.routes import router as search_router
from modules.settings.onboarding_routes import router as onboarding_router
from modules.settings.routes import router as settings_router
from modules.sources.routes import router as sources_router
from modules.tasks.routes import router as tasks_router
from modules.tasks.tools import register_task_tools
from modules.timeline.routes import router as timeline_router
from modules.tools.browser import register_browser_tool
from modules.tools.browser_control import router as browser_control_router
from modules.tools.builtins import register_builtin_tools
from modules.tools.mcp_management_routes import router as mcp_management_router
from modules.tools.public import McpAdmission, McpRuntime, create_inbound_mcp_bundle
from modules.tools.routes import browser_jobs_router
from modules.tools.routes import router as tools_router
from modules.tools.webhook import register_webhook_tool


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the API, owner security middleware, domain services and canonical tool registry.

    Args:
        settings: Optional settings override; absent uses validated process configuration.
    Returns:
        FastAPI application with lifespan-owned database/Redis clients and app-scoped modules.
    Side effects:
        Creates clients and registers only tool names contributed by enabled descriptors. When
        agents has one fixed owner-authenticated REST workflow; when tools is enabled, it composes
        shared admission/runtime callbacks, owner routes before the
        exact slash-terminated inbound mount, and one root-managed SDK session manager. Lifespan
        hydrates at most the runtime's bounded owner catalog and drains admission before manager
        exit; Redis and SQLAlchemy disposal run even when startup hydration fails.
    """
    app_settings = settings or Settings()
    csrf_secret = app_settings.csrf_signing_secret.get_secret_value()
    workers_raw = os.getenv("WEB_CONCURRENCY", "1").strip() or "1"
    if not workers_raw.isdigit():
        raise RuntimeError(f"WEB_CONCURRENCY must be a positive integer, got {workers_raw!r}")
    if not csrf_secret and int(workers_raw) > 1:
        raise RuntimeError("CSRF_SIGNING_SECRET is required when WEB_CONCURRENCY > 1 (each worker would sign with a different secret)")
    engine, session_factory = make_session_factory(
        app_settings.database_url,
        pool_size=app_settings.db_pool_size,
        max_overflow=app_settings.db_max_overflow,
        statement_timeout_ms=app_settings.db_statement_timeout_ms,
        idle_tx_timeout_ms=app_settings.db_idle_tx_timeout_ms,
    )
    # ArqRedis subclasses Redis, so existing callers work and enqueue_job exists for chat/agent dispatch.
    redis = ArqRedis.from_url(
        app_settings.redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=2,
        health_check_interval=30,
        max_connections=100,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the SDK manager once, hydrate bounded owner state, and always close shared clients.

        Admission is stopped before the SDK manager drains; Redis and SQLAlchemy are closed even
        when manager entry or sequential owner hydration fails during startup.
        """
        try:
            async with AsyncExitStack() as stack:
                try:
                    bundle = getattr(_app.state, "mcp_bundle", None)
                    if bundle is not None:
                        await stack.enter_async_context(bundle.server.session_manager.run())
                        await _app.state.mcp_runtime.hydrate_connections(owner_id=1)
                    yield
                finally:
                    admission = getattr(_app.state, "mcp_admission", None)
                    if admission is not None:
                        admission.stop()
        finally:
            try:
                await redis.aclose()
            finally:
                await engine.dispose()

    app = FastAPI(title="BBD-OS", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.session_factory = session_factory
    app.state.realtime_connections = asyncio.Semaphore(MAX_STREAMS_PER_API_PROCESS)
    app.state.redis = redis
    app.add_middleware(
        SessionMiddleware,
        secret_key=csrf_secret or secrets.token_urlsafe(32),
        https_only=app_settings.secure_cookies,
        same_site="lax",
    )
    install_log_redaction()
    install_error_handling(app)
    app.add_middleware(BackupActivityMiddleware)
    app.add_middleware(
        BodyLimitMiddleware,
        default_limit=app_settings.max_request_body_bytes,
        upload_limit=app_settings.upload_max_bytes + 1024 * 1024,
    )
    app.include_router(auth_router)
    app.include_router(backup_router)
    app.include_router(export_router)
    app.include_router(system_router)
    app.include_router(sources_router)
    app.include_router(documents_router)
    app.include_router(observations_router)
    app.include_router(entities_router)
    app.include_router(relationships_router)
    app.include_router(timeline_router)
    app.include_router(temporal_router)
    app.include_router(document_upload_router)
    app.include_router(ingestion_router)
    app.include_router(connectors_router)
    app.include_router(browser_control_router)
    app.include_router(connector_provisioning_router)
    app.include_router(github_oauth_router)
    app.include_router(settings_router)
    app.include_router(onboarding_router)
    app.include_router(model_gateway_router)
    app.include_router(search_router)
    app.include_router(dashboard_router)
    app.include_router(realtime_router)
    app.include_router(tasks_router)
    app.include_router(goals_router)
    app.include_router(topics_router)
    app.include_router(news_router)
    app.include_router(notifications_router)
    app.include_router(automations_router)
    app.include_router(automation_webhook_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    app.include_router(agents_router)
    app.include_router(observability_router)
    app.include_router(operations_router)
    app.include_router(browser_jobs_router)
    app.state.modules = register_modules()
    tool_registry = ToolRegistry(module_registry=app.state.modules)
    enabled_descriptors = [item for item in app.state.modules.values() if item.enabled]
    declared_tools = {name for item in enabled_descriptors for name in item.tools}
    if app.state.modules["tools"].enabled and declared_tools:
        register_builtin_tools(tool_registry, frozenset(declared_tools))
        register_webhook_tool(tool_registry, app_settings)
        register_browser_tool(tool_registry)
        register_handoff_tool(tool_registry, frozenset(declared_tools))
        register_task_tools(tool_registry, frozenset(declared_tools))
        register_goal_tools(tool_registry, frozenset(declared_tools))
        register_automation_tools(tool_registry, frozenset(declared_tools))
    app.state.tool_registry = tool_registry
    if app.state.modules["tools"].enabled:
        app.include_router(tools_router)
        app.include_router(mcp_management_router)
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


    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return the process liveness response."""
        return {"status": "ok"}

    return app
