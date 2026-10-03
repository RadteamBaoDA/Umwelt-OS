from collections.abc import AsyncIterator
import asyncio
from contextlib import asynccontextmanager
import secrets

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.auth.routes import router as auth_router
from core.config import Settings
from core.errors import install_error_handling
from core.modules import register_modules
from core.system.routes import router as system_router
from modules.knowledge.documents.routes import router as documents_router
from modules.knowledge.entities.routes import router as entities_router
from modules.knowledge.relationships.routes import router as relationships_router
from modules.ingestion.routes import documents_router as document_upload_router
from modules.ingestion.routes import router as ingestion_router
from modules.sources.routes import router as sources_router
from modules.connectors.routes import router as connectors_router
from modules.connectors.provisioning_routes import router as connector_provisioning_router
from modules.settings.routes import router as settings_router
from modules.model_gateway.routes import router as model_gateway_router
from modules.search.routes import router as search_router
from core.realtime_routes import router as realtime_router
from modules.timeline.routes import router as timeline_router
from modules.knowledge.temporal.routes import router as temporal_router
from modules.chat.routes import router as chat_router
from modules.memory.routes import router as memory_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the FastAPI app, lifespan-managed clients, middleware, routers, realtime capacity limit, and module registry."""
    app_settings = settings or Settings()
    engine = create_async_engine(app_settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=0)
    redis = Redis.from_url(app_settings.redis_url, decode_responses=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Dispose Redis and the database engine when the API application shuts down."""
        yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="BBD-OS", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.realtime_connections = asyncio.Semaphore(4)
    app.state.redis = redis
    app.add_middleware(
        SessionMiddleware,
        secret_key=(
            app_settings.csrf_signing_secret.get_secret_value()
            or secrets.token_urlsafe(32)
        ),
        https_only=app_settings.secure_cookies,
        same_site="lax",
    )
    install_error_handling(app)
    app.include_router(auth_router)
    app.include_router(system_router)
    app.include_router(sources_router)
    app.include_router(documents_router)
    app.include_router(entities_router)
    app.include_router(relationships_router)
    app.include_router(timeline_router)
    app.include_router(temporal_router)
    app.include_router(document_upload_router)
    app.include_router(ingestion_router)
    app.include_router(connectors_router)
    app.include_router(connector_provisioning_router)
    app.include_router(settings_router)
    app.include_router(model_gateway_router)
    app.include_router(search_router)
    app.include_router(realtime_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    app.state.modules = register_modules()


    @app.get("/health")
    async def health() -> dict[str, str]:
        """Return the process liveness response."""
        return {"status": "ok"}

    return app
