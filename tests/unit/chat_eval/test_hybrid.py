from __future__ import annotations

from uuid import uuid4

import pytest

from modules.chat.schemas import AnswerContext, EvidenceItem
from tests.integration.chat_eval.cases import Case
from tests.integration.chat_eval.hybrid import (
    drain,
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


@pytest.mark.asyncio
async def test_drain_sums_passes_until_zero() -> None:
    results = iter([3, 2, 0])

    async def step() -> int:
        return next(results)

    assert await drain(step) == 5


@pytest.mark.asyncio
async def test_drain_raises_when_work_never_ends() -> None:
    async def step() -> int:
        return 1

    with pytest.raises(AssertionError, match="not drained after 4 passes"):
        await drain(step, bound=4)
