"""Chat's public one-shot web search for background collectors (no conversation involved).

Collectors such as Topic watch run in the worker without a chat run. They reuse the owner's web
search consent, endpoint and daily quota through this single command so no other module imports
chat internals.
"""

from dataclasses import dataclass
from typing import Literal

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.schemas import WebSearchTarget
from core.model_gateway.transport import (
    approved_web_search_transport,
)
from core.workspaces.admission import actor_id
from core.workspaces.schemas import Scope
from modules.chat import web_search as web
from modules.chat.web_search import WebSearchResult
from modules.settings import public as settings_public

SearchError = Literal[
    "not_configured",
    "daily_limit",
    "timeout",
    "provider_error",
    "network_denied",
    "query_too_long",
    "empty_query",
]


@dataclass(frozen=True)
class CollectorSearchOutcome:
    """Result of one consented, quota-charged web search; ``error`` is a bounded code or None."""

    results: tuple[WebSearchResult, ...]
    provider: str | None
    error: SearchError | None


def _failed(error: SearchError) -> CollectorSearchOutcome:
    """Build the empty outcome that carries one bounded failure code."""
    return CollectorSearchOutcome(results=(), provider=None, error=error)


async def search_web_for_collection(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    query: str,
    *,
    scope: Scope,
) -> CollectorSearchOutcome:
    """One web search on behalf of a background collector; never raises for provider failures.

    Order: normalise the query (empty or over-long is refused before any lock), then in one short
    transaction lock ``ai_settings`` FOR SHARE, resolve the targets the owner's privacy consent
    permits (primary plus fallbacks) and charge the shared per-owner daily quota (the same key as
    chat, so the owner's limit is shared); commit, so no lock is held across egress; then call the
    providers in order through the approved, network-pinned transport. Failures map to the bounded
    codes of ``CollectorSearchOutcome``. The query, endpoint and key are never logged. The quota is
    charged even when the provider later fails, as in chat.
    """
    try:
        normalized = web.normalize_query(query)
    except web.WebSearchError as exc:
        return _failed(
            exc.code if exc.code in ("empty_query", "query_too_long") else "provider_error"
        )
    async with session_factory() as session:
        if not await settings_public.lock_ai_settings_for_share(session, scope=scope):
            await session.rollback()
            return _failed("not_configured")
        config = await settings_public.get_ai_execution_config(
            session, settings, redis, scope=scope
        )
        primary = WebSearchTarget(
            provider=config.web_search_provider,
            endpoint=config.web_search_endpoint,
            api_key=config.web_search_api_key,
            credential_configured=config.web_search_credential_configured,
        )
        targets = [
            target
            for target in (primary, *config.web_search_fallbacks)
            if web.target_permitted(target, config.privacy)
        ]
        if not targets:
            await session.rollback()
            return _failed("not_configured")
        if not await web.consume_daily_quota(
            redis,
            f"{scope.workspace_id}:{actor_id(scope)}",
            settings.web_search_daily_limit,
        ):
            await session.rollback()
            return _failed("daily_limit")
        await session.commit()  # release the settings lock before any network wait

    def make_transport(candidate: WebSearchTarget) -> httpx.AsyncBaseTransport:
        """Pin the candidate endpoint to the approved network policy (same rule as chat)."""
        if candidate.endpoint is None:
            raise RuntimeError("invariant violated: candidate.endpoint must not be None")
        return approved_web_search_transport(
            candidate.endpoint,
            settings.ai_allowed_endpoint_hosts,
            settings.web_search_allowed_cidrs,
            allow_http=candidate.provider == "searxng",
        )

    try:
        results, provider = await web.search_with_fallback(targets, normalized, make_transport)
    except web.WebSearchError as exc:
        return _failed(exc.code)
    return CollectorSearchOutcome(results=tuple(results), provider=provider, error=None)
