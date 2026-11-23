from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from arq.worker import Retry

from modules.chat.schemas import AnswerContext, EvidenceItem
from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.hybrid import (
    drain_indexing,
    hybrid_env,
    local_only_blocked,
    mask_secrets,
    retrieval_request,
)

EMBED = {
    "E2E_REAL_BASE_URL": "https://chat.example/v1",
    "E2E_REAL_MODEL": "chat-model",
    "E2E_REAL_API_KEY": "chat-secret",
    "E2E_REAL_EMBED_BASE_URL": "https://embed.example/v1/",
    "E2E_REAL_EMBED_MODEL": "embed-model",
    "E2E_REAL_EMBED_API_KEY": "embed-secret",
}
RERANK = {
    "E2E_REAL_RERANK_BASE_URL": "https://rerank.example/v1",
    "E2E_REAL_RERANK_MODEL": "rerank-model",
    "E2E_REAL_RERANK_API_KEY": "rerank-secret",
}


def _item(*, local_only: bool) -> EvidenceItem:
    """One evidence item with the fields the guard reads; the rest is fixed fixture data."""
    return EvidenceItem(
        source_id=uuid4(),
        source_generation=1,
        local_only=local_only,
        document_id=uuid4(),
        document_version_id=uuid4(),
        version_number=1,
        chunk_id=uuid4(),
        content="x",
        title="t",
    )


def test_hybrid_env_is_none_without_embedding_settings() -> None:
    assert hybrid_env({}) is None
    assert hybrid_env({**EMBED, "E2E_REAL_EMBED_API_KEY": ""}) is None


def test_embed_only_env_runs_with_rerank_off() -> None:
    h = hybrid_env(EMBED)
    assert h is not None
    assert h.rerank is None and h.rerank_label == "off"
    assert h.embed.base_url == "https://embed.example/v1"  # trailing slash stripped


def test_full_env_enables_rerank() -> None:
    h = hybrid_env({**EMBED, **RERANK})
    assert h is not None and h.rerank is not None
    assert h.rerank.model == "rerank-model" and h.rerank_label == "on"


def test_mask_secrets_hides_every_api_key_value() -> None:
    text = "failed for chat-secret and embed-secret; model name chat-model stays"
    masked = mask_secrets(text, {**EMBED, **RERANK})
    assert "chat-secret" not in masked and "embed-secret" not in masked
    assert "chat-model" in masked


def test_local_only_guard_blocks_remote_turn() -> None:
    assert local_only_blocked(AnswerContext(query="q", evidence=[_item(local_only=True)]))
    assert not local_only_blocked(AnswerContext(query="q", evidence=[_item(local_only=False)]))
    assert not local_only_blocked(AnswerContext(query="q", evidence=[]))


def test_retrieval_request_pins_scope_when_scoped() -> None:
    case = Case("c", "cat", "en", "q?")
    unscoped = retrieval_request(case, None)
    assert unscoped.source_scope == [] and unscoped.query == "q?"
    source = str(uuid4())
    scoped = retrieval_request(case, [source])
    assert [str(s) for s in scoped.source_scope] == [source]


def _scripted(values: list[int | BaseException]) -> Callable[[], Awaitable[int]]:
    """A pass function replaying ``values``; an exception entry is raised instead of returned."""
    queue = list(values)

    async def step() -> int:
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    return step


async def _no_sleep(_seconds: float) -> None:
    """Stand-in pause so the drain loop runs without real waiting."""


@pytest.mark.asyncio
async def test_drain_indexing_returns_count_after_retry_and_drained_pass() -> None:
    # Pass 1 finds nothing yet, pass 2 is deferred by the worker's heavy slot, pass 3 drains.
    step = _scripted([0, Retry(), 0])
    counts = _scripted_counts([0, 0, 5])

    async def describe() -> str:
        return "unused"

    indexed = await drain_indexing(step, counts, describe, pause=_no_sleep)

    assert indexed == 5


@pytest.mark.asyncio
async def test_drain_indexing_does_not_exit_on_zero_work_with_nothing_indexed() -> None:
    # A drained step with no succeeded chunks is not a finished run: keep polling until bound.
    async def describe() -> str:
        return "status=failed error_code=model_unavailable"

    with pytest.raises(AssertionError, match="model_unavailable"):
        await drain_indexing(
            _scripted([0, 0, 0]), _scripted_counts([0, 0, 0]), describe, bound=3, pause=_no_sleep
        )


@pytest.mark.asyncio
async def test_drain_indexing_raises_when_work_never_ends() -> None:
    async def describe() -> str:
        return "status=active error_code=None"

    with pytest.raises(AssertionError, match="not drained after 4 passes.*status=active"):
        await drain_indexing(
            _scripted([1, 1, 1, 1]),
            _scripted_counts([1, 1, 1, 1]),
            describe,
            bound=4,
            pause=_no_sleep,
        )


def _scripted_counts(values: list[int]) -> Callable[[], Awaitable[int]]:
    """A count function replaying ``values`` one per pass."""
    queue = list(values)

    async def count() -> int:
        return queue.pop(0)

    return count
