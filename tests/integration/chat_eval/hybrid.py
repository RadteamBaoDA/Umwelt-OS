"""Hybrid retrieval leg of the chat real-model eval: real embeddings, rerank, build_context and follow-up rewrite.

The ``tool`` leg gives the model no retrieved evidence. The hybrid leg reproduces what the production worker does
before a turn: rewrite a follow-up query, call ``build_context`` (search, embeddings, permitted rerank) and fail closed
when any evidence item is ``local_only``. Everything here is test-only and runs on the disposable test database
with real Redis and Settings. Endpoints and keys come from ``.env.e2e-real`` at runtime; the keys are stored only
through AI settings in that disposable database and are never printed, logged or written to the report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.config import Settings
from core.worker_cursors import STATE_KEY
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.chat._worker.setup import _rewrite_for_retrieval
from modules.chat._worker.state import _RunCtx, _TurnInputs
from modules.chat.retrieval import build_context
from modules.chat.schemas import AnswerContext, AnswerContextRequest
from modules.search.indexing import index_pending_chunks
from tests.integration.chat_eval.cases import Case

INDEX_PASS_BOUND = 200  # index_pending_chunks passes before the run counts as not drained
SEARCH_MODULE_ID = "search"


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible endpoint from ``.env.e2e-real``. ``api_key`` is secret: never print it."""

    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class HybridEnv:
    """Endpoints for the hybrid leg: the chat model (also used by the follow-up rewrite), the embedding
    model (required) and the rerank model (optional; without it the hybrid leg runs with ``rerank: off``)."""

    chat: Endpoint
    embed: Endpoint
    rerank: Endpoint | None

    @property
    def rerank_label(self) -> str:
        """Report header value: ``on`` when a rerank endpoint is configured, otherwise ``off``."""
        return "on" if self.rerank is not None else "off"


def _endpoint(env: Mapping[str, str], prefix: str) -> Endpoint | None:
    """Read ``<prefix>_BASE_URL/_MODEL/_API_KEY``; None unless all three are non-blank."""
    base = env.get(f"{prefix}_BASE_URL", "").strip()
    model = env.get(f"{prefix}_MODEL", "").strip()
    key = env.get(f"{prefix}_API_KEY", "").strip()
    if not (base and model and key):
        return None
    return Endpoint(base.rstrip("/"), model, key)


def hybrid_env(env: Mapping[str, str]) -> HybridEnv | None:
    """Return the hybrid configuration from ``env`` (pure), or None when the leg cannot run.

    The leg needs the chat endpoint and the embedding endpoint. A missing rerank endpoint is not an error: the leg
    runs with ``rerank`` set to None.
    """
    chat = _endpoint(env, "E2E_REAL")
    embed = _endpoint(env, "E2E_REAL_EMBED")
    if chat is None or embed is None:
        return None
    return HybridEnv(chat=chat, embed=embed, rerank=_endpoint(env, "E2E_REAL_RERANK"))


def mask_secrets(text: str, env: Mapping[str, str]) -> str:
    """Replace every non-empty ``E2E_*API_KEY`` value found in ``env`` inside ``text`` with ``***`` (pure)."""
    for name, value in env.items():
        if name.startswith("E2E_") and name.endswith("API_KEY") and value:
            text = text.replace(value, "***")
    return text


def local_only_blocked(ctx: AnswerContext) -> bool:
    """True when the turn must not reach the remote model: some evidence item is ``local_only`` (pure).

    Mirrors the production rule in ``_build_model_setup``: a local-only item switches the turn to local-only,
    and the eval model is remote, so such a turn is recorded as blocked instead of being sent.
    """
    return any(item.local_only for item in ctx.evidence)


def retrieval_request(case: Case, scope: list[str] | None) -> AnswerContextRequest:
    """Build the turn's ``AnswerContextRequest`` for a case, pinned to ``scope`` when the case is scoped (pure)."""
    if scope is None:
        return AnswerContextRequest(query=case.q)
    return AnswerContextRequest(
        query=case.q, source_scope=[UUID(s) for s in scope], source_scope_enforced=True
    )


def _provider(pid: str, endpoint: Endpoint, model_type: str) -> dict[str, Any]:
    """One provider body for AI settings: a custom endpoint listing its single model with ``model_type``."""
    return {
        "id": pid,
        "kind": "custom",
        "label": pid,
        "base_url": endpoint.base_url,
        "credential_action": "replaced",
        "api_key": endpoint.api_key,
        "models": [{"id": endpoint.model, "type": model_type}],
    }


def _providers(h: HybridEnv) -> list[dict[str, Any]]:
    """The providers the hybrid leg stores: chat, embedding and (when configured) rerank."""
    providers = [_provider("chat", h.chat, "llm"), _provider("embed", h.embed, "embedding")]
    if h.rerank is not None:
        providers.append(_provider("rerank", h.rerank, "reranker"))
    return providers


def _key_env(h: HybridEnv) -> dict[str, str]:
    """The endpoint keys of ``h`` under their ``E2E_*API_KEY`` names, so ``mask_secrets`` can scrub error text."""
    env = {"E2E_REAL_API_KEY": h.chat.api_key, "E2E_REAL_EMBED_API_KEY": h.embed.api_key}
    if h.rerank is not None:
        env["E2E_REAL_RERANK_API_KEY"] = h.rerank.api_key
    return env


async def _probe_embedding_dimensions(client: AsyncClient, h: HybridEnv) -> int:
    """Probe the embedding endpoint with an unsaved draft and return its vector size (the role needs it).

    Raises AssertionError with the probe result only (the gateway detail is masked) when the probe is not supported.
    """
    response = await client.post(
        "/api/v1/settings/models/draft-test",
        json={
            "role": "embedding",
            "capability": "embeddings",
            "base_url": h.embed.base_url,
            "kind": "custom",
            "api_key": h.embed.api_key,
            "model": h.embed.model,
        },
    )
    body = response.json()
    if response.status_code != 200 or body.get("result") != "supported":
        detail = mask_secrets(str(body.get("detail", ""))[:200], _key_env(h))
        raise AssertionError(
            f"embedding probe {body.get('result', response.status_code)}: {detail}"
        )
    dimensions = body.get("dimensions")
    if not isinstance(dimensions, int):
        raise AssertionError("embedding probe returned no dimensions")
    return dimensions


def _consent_privacy(destinations: dict[str, str], h: HybridEnv) -> dict[str, Any]:
    """Privacy block granting reasoning to the chat destination and embeddings to embed and rerank destinations."""
    embedding = [destinations["embed"]]
    if h.rerank is not None:
        embedding.append(destinations["rerank"])  # reranking is gated by the embedding consent
    return {
        "allow_remote_reasoning": True,
        "reasoning_destinations": [destinations["chat"]],
        "allow_remote_embeddings": True,
        "embedding_destinations": list(dict.fromkeys(embedding)),
    }


async def configure_ai(client: AsyncClient, h: HybridEnv) -> None:
    """Store the chat, embedding and rerank providers and their roles, then grant consent to their destinations.

    Three steps, because a role's embedding needs probed dimensions and consent is bound to a destination that only
    exists once the provider is stored: (1) PUT the providers with the chat role; (2) probe the embedding dimensions
    through an unsaved draft; (3) PUT the embedding and rerank roles with the dimensions and the consent grants.
    Reranking is gated by the embedding consent (``may_send`` for capability ``reranking``). Raises AssertionError
    when a save or the probe is refused.
    """
    current = (await client.get("/api/v1/settings/ai")).json()
    common = {"request_timeout_seconds": 60}
    chat_role = {"model": h.chat.model, "destination": "remote", "provider_id": "chat"}
    first = await client.put(
        "/api/v1/settings/ai",
        json={
            **common,
            "defaults": {"chat": chat_role},
            "providers": _providers(h),
            "expected_revision": current["configuration_revision"],
        },
    )
    assert first.status_code == 200, first.text[:300]
    stored = first.json()
    destinations = {p["id"]: p["destination_id"] for p in stored["providers"]}
    assert all(destinations.values()), "every provider needs a destination"
    dimensions = await _probe_embedding_dimensions(client, h)
    defaults: dict[str, Any] = {
        "chat": chat_role,
        "embedding": {
            "model": h.embed.model,
            "destination": "remote",
            "provider_id": "embed",
            "dimensions": dimensions,
        },
    }
    if h.rerank is not None:
        defaults["reranker"] = {
            "model": h.rerank.model,
            "destination": "remote",
            "provider_id": "rerank",
        }
    saved = await client.put(
        "/api/v1/settings/ai",
        json={
            **common,
            "defaults": defaults,
            "privacy": _consent_privacy(destinations, h),
            "expected_revision": stored[
                "configuration_revision"
            ],  # providers omitted = keep stored
        },
    )
    assert saved.status_code == 200, saved.text[:300]


async def enable_search(client: AsyncClient) -> None:
    """Make sure the ``search`` module is enabled for the owner's workspace (the indexer skips it otherwise)."""
    modules = (await client.get("/api/v1/settings/modules")).json()
    if any(m["id"] == SEARCH_MODULE_ID and m["enabled"] for m in modules["modules"]):
        return
    response = await client.patch(
        "/api/v1/settings/modules",
        json={
            "expected_revision": modules["configuration_revision"],
            "module_id": SEARCH_MODULE_ID,
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text[:300]


async def drain(step: Callable[[], Awaitable[int]], bound: int = INDEX_PASS_BOUND) -> int:
    """Call ``step`` until it returns 0 and return the sum of its positive results (pure orchestration).

    Raises AssertionError when ``bound`` passes still return work: the run must be drained before the hybrid
    turns read the index, otherwise evidence would silently be missing.
    """
    total = 0
    for _ in range(bound):
        done = await step()
        if done == 0:
            return total
        total += done
    raise AssertionError(f"indexing not drained after {bound} passes")


async def index_all(engine: AsyncEngine, settings: Settings, redis: Redis) -> int:
    """Index every pending chunk of the eval workspace; return how many were indexed.

    The context matches the worker's (session factory, Redis, Settings and the cursor state dict that ARQ keeps
    across job copies). Passes run until one returns 0, bounded by ``INDEX_PASS_BOUND``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ctx: dict[str, object] = {
        "session_factory": factory,
        "redis": redis,
        "settings": settings,
        STATE_KEY: {},
    }

    async def step() -> int:
        """One indexing pass over the worker's job context."""
        return await index_pending_chunks(ctx)

    return await drain(step)


async def hybrid_context(
    case: Case,
    request: AnswerContextRequest,
    scope: WorkspaceContext,
    fence: AccessFence,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
) -> AnswerContext:
    """Return the evidence the production worker would hand the model for this case (a DB and provider call).

    A follow-up (``case.history`` non-empty) is first rewritten through the production
    ``_rewrite_for_retrieval`` (the chat model is called). Then ``build_context`` runs search, embeddings and the
    permitted rerank. As in production, a rewritten query is replaced by the owner's question in the result.
    """
    run = _RunCtx(
        response_id=uuid4(),
        session_factory=factory,
        settings=settings,
        redis=redis,
        conversation_id=uuid4(),
        privacy_fence=None,
        seq=0,
        job_scope=scope,  # type: ignore[arg-type]  # the eval runs as the owner, like the tool leg
        access_fence=fence,
        answer_context=AnswerContext(query=case.q),
    )
    prior: list[dict[str, str]] = []
    for user_text, assistant_text in case.history:
        prior += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    turn = SimpleNamespace(
        user_prompt=case.q,
        prior_messages=prior,
        req_params={},
        answer_request=request,
    )
    async with factory() as session:
        rewritten, _tz = await _rewrite_for_retrieval(
            session, run, cast(_TurnInputs, turn), web_requested=False
        )
        context = await build_context(session, factory, redis, settings, rewritten, scope)
        await session.commit()
    if rewritten is not request:
        context = context.model_copy(update={"query": case.q})
    return context
