"""Hybrid retrieval leg of the chat real-model eval: real embeddings, rerank, build_context and follow-up rewrite.

The ``tool`` leg gives the model no retrieved evidence. The hybrid leg reproduces what the production worker does
before a turn: rewrite a follow-up query, call ``build_context`` (search, embeddings, permitted rerank) and fail closed
when any evidence item is ``local_only``. Everything here is test-only and runs on the disposable test database
with real Redis and Settings. Endpoints and keys come from ``.env.e2e-real`` at runtime; the keys are stored only
through AI settings in that disposable database and are never printed, logged or written to the report.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from arq.worker import Retry
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.config import Settings
from core.model_gateway.client import ModelGateway
from core.worker_cursors import STATE_KEY
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.chat._worker.setup import _rewrite_for_retrieval
from modules.chat._worker.state import _RunCtx, _TurnInputs
from modules.chat.retrieval import build_context
from modules.chat.schemas import AnswerContext, AnswerContextRequest
from modules.search.indexing import index_pending_chunks
from tests.integration.chat_eval.cases import Case

# Index passes before the run counts as not drained (about 2 minutes at the default pause below).
INDEX_PASS_BOUND = int(os.getenv("CHAT_EVAL_INDEX_PASSES", "60"))
# Consecutive heavy-slot deadline expiries after which the build is treated as stuck, not waiting.
TIMEOUT_STREAK_LIMIT = 3
INDEX_PASS_SLEEP_SECONDS = 2.0  # pause between passes so a heavy slot held by the worker can clear
SEARCH_MODULE_ID = "search"


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible endpoint from ``.env.e2e-real``. ``api_key`` is secret: never print it."""

    base_url: str
    model: str
    api_key: str = field(repr=False)  # secret: kept out of reprs and pytest tracebacks


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


async def drain_indexing(
    step: Callable[[], Awaitable[int]],
    count: Callable[[], Awaitable[int]],
    describe: Callable[[], Awaitable[str]],
    *,
    bound: int = INDEX_PASS_BOUND,
    pause: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> int:
    """Run indexing passes until the run is drained and something is indexed; return the indexed count.

    Each pass calls ``step`` (returns the chunks it indexed, or raises ``arq.Retry`` when the worker's heavy slot
    defers it, or TimeoutError when waiting for that slot expires) and then ``count`` (succeeded chunks of the active generation, including those the cron worker
    indexed). The run is drained when a pass completed without a Retry and returned 0, and it is finished once
    ``count`` is positive. Waits ``pause`` seconds after each unfinished pass. Raises AssertionError after
    ``bound`` passes, with ``describe()`` (the latest generation's status and error code) in the message.

    A TimeoutError means the pass hit the heavy-slot deadline. Consecutive TimeoutErrors are counted: after
    ``TIMEOUT_STREAK_LIMIT`` in a row the build is stuck (a concurrent HNSW build blocked on a lock), so this raises
    at once with ``describe()`` instead of waiting out the bound. A Retry neither counts nor resets the streak; any
    pass that completes resets it.
    """
    timeouts = 0
    for _ in range(bound):
        try:
            drained = await step() == 0
            timeouts = 0
        except Retry:
            # Deferred by the worker's heavy slot: this pass did no work, so it cannot prove the run is drained.
            drained = False
        except TimeoutError:
            # The slot wait expired (e.g. while the worker builds the HNSW index): no work either.
            drained = False
            timeouts += 1
            if timeouts >= TIMEOUT_STREAK_LIMIT:
                detail = await describe()
                raise AssertionError(
                    f"index build cancelled at the heavy-slot deadline {timeouts} times in a row: {detail}"
                ) from None
        indexed = await count()
        if drained and indexed > 0:
            return indexed
        await pause(INDEX_PASS_SLEEP_SECONDS)
    detail = await describe()
    raise AssertionError(f"indexing not drained after {bound} passes: {detail}")


_ACTIVE_INDEXED_SQL = text(
    "SELECT count(*) FROM search_index_items i "
    "JOIN search_index_generations g ON g.id = i.generation_id "
    "WHERE g.status = 'active' AND i.status = 'succeeded'"
)
_LATEST_GENERATION_SQL = text(
    "SELECT status, error_code FROM search_index_generations "
    "ORDER BY created_at DESC, id DESC LIMIT 1"
)
_ITEM_ERRORS_SQL = text(
    "SELECT status, coalesce(error_code, '-'), count(*) FROM search_index_items "
    "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8"
)
_OTHER_SESSIONS_SQL = text(
    "SELECT pid, application_name, state, wait_event_type, wait_event, backend_xmin, "
    "now() - xact_start, left(query, 60) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
)
_INDEX_LOCKS_SQL = text(
    "SELECT pid, locktype, mode, granted FROM pg_locks "
    "WHERE locktype = 'advisory' OR relation = 'search_index_items'::regclass"
)


async def _session_and_lock_summary(engine: AsyncEngine) -> str:
    """Compact view of the other sessions and the index-relevant locks, for a stuck-index message.

    Runs on its own connection. It reads ``pg_stat_activity`` for this database (excluding this session) and the
    advisory and ``search_index_items`` locks from ``pg_locks``. The output is plain text; the caller masks it.
    """
    async with engine.connect() as conn:
        sessions = (await conn.execute(_OTHER_SESSIONS_SQL)).all()
        locks = (await conn.execute(_INDEX_LOCKS_SQL)).all()
    session_text = "; ".join(
        f"pid={pid} app={app} state={state} wait={wait_type}/{wait_event} xmin={xmin} "
        f"xact_age={age} q={query!r}"
        for pid, app, state, wait_type, wait_event, xmin, age, query in sessions
    )
    lock_text = "; ".join(
        f"pid={pid} {locktype} mode={mode} granted={granted}"
        for pid, locktype, mode, granted in locks
    )
    return f"sessions: [{session_text or 'none'}]; locks: [{lock_text or 'none'}]"


async def index_all(
    engine: AsyncEngine,
    settings: Settings,
    redis: Redis,
    mask: Callable[[str], str] = lambda text_: text_,
) -> int:
    """Index every pending chunk of the eval workspace; return how many succeeded in the active generation.

    The context matches the worker's (session factory, Redis, Settings and the cursor state dict that ARQ keeps
    across job copies). The cron worker indexes the same rows every 60 s, so the result is read from the database
    rather than summed from the passes. Passes stop as ``drain_indexing`` describes.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ctx: dict[str, object] = {
        "session_factory": factory,
        "redis": redis,
        "settings": settings,
        STATE_KEY: {},
    }

    first_error: list[str] = []
    real_embed = ModelGateway.embed

    async def recording_embed(self: ModelGateway, *args: Any, **kwargs: Any) -> Any:
        """The real gateway call; remembers the first failure (masked) so a timeout can name it."""
        try:
            return await real_embed(self, *args, **kwargs)
        except Exception as exc:
            if not first_error:
                first_error.append(mask(f"{type(exc).__name__}: {exc}")[:300])
            raise

    async def step() -> int:
        """One indexing pass over the worker's job context, with embed failures recorded."""
        ModelGateway.embed = recording_embed  # type: ignore[method-assign]
        try:
            return await index_pending_chunks(ctx)
        finally:
            ModelGateway.embed = real_embed  # type: ignore[method-assign]

    async def count() -> int:
        """Succeeded chunks of the active generation, read on a fresh connection."""
        async with engine.connect() as conn:
            return int((await conn.execute(_ACTIVE_INDEXED_SQL)).scalar_one())

    async def describe() -> str:
        """Latest generation's status, error code, item breakdown and session/lock summary, masked, for failures.

        Reads the database on fresh connections, so it works while the worker holds the index build. The output
        contains no key values: ``mask`` removes them from the whole message.
        """
        async with engine.connect() as conn:
            row = (await conn.execute(_LATEST_GENERATION_SQL)).one_or_none()
            items = (await conn.execute(_ITEM_ERRORS_SQL)).all()
        if row is None:
            return "no search index generation exists"
        breakdown = ", ".join(f"{status}/{code}={n}" for status, code, n in items)
        cause = f"; first embed error: {first_error[0]}" if first_error else ""
        summary = await _session_and_lock_summary(engine)
        return mask(
            f"latest generation status={row[0]} error_code={row[1]}; items: {breakdown or 'none'}"
            f"{cause}; {summary}"
        )

    return await drain_indexing(step, count, describe)


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
