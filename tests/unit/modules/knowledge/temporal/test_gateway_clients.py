"""Characterization tests for the gateway-backed Graphiti clients built by ``_make_clients``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.model_gateway.client import ModelGatewayError
from modules.knowledge.temporal.adapter import GraphOperationError, _make_clients


def _context(dimensions: int | None = 3, reranking: bool = True, authorize: Any = None) -> Any:
    """Minimal authorization stand-in exposing only what the clients read."""
    model = SimpleNamespace(role="r", mapping=object(), policy=object(), dimensions=dimensions)
    return SimpleNamespace(
        reasoning=model, small_reasoning=SimpleNamespace(role="small", mapping=object(), policy=object()),
        embedding=model, reranking=model if reranking else None, model_calls=[0],
        authorize=authorize or AsyncMock(),
    )


def _completion(text: str) -> dict[str, Any]:
    """Wrap text in an OpenAI-compatible chat completion."""
    return {"choices": [{"message": {"content": text}}]}


@pytest.mark.asyncio
async def test_llm_chat_parses_json_and_authorizes_before_each_send() -> None:
    """Plain chat returns parsed JSON and calls before_send, which counts against the model budget."""
    context = _context()
    gateway = SimpleNamespace(chat=AsyncMock(), structured=AsyncMock())

    async def chat(*_a: Any, before_send: Any, max_tokens: int, **_k: Any) -> dict[str, Any]:
        await before_send()
        assert max_tokens == 4096
        return _completion(json.dumps({"ok": 1}))

    gateway.chat.side_effect = chat
    llm, _, _ = _make_clients(context, gateway)
    message = SimpleNamespace(role="user", content="hi")
    assert await llm._generate_response([message], max_tokens=99999) == {"ok": 1}
    context.authorize.assert_awaited_once_with("reasoning")
    assert context.model_calls == [1]


@pytest.mark.asyncio
async def test_llm_uses_small_model_and_structured_schema() -> None:
    """model_size small selects the small policy; a response model goes through structured()."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: int

    context = _context()
    gateway = SimpleNamespace(structured=AsyncMock(return_value=_completion('{"value": 2}')))
    llm, _, _ = _make_clients(context, gateway)
    result = await llm._generate_response(
        [SimpleNamespace(role="user", content="x")], response_model=Out, max_tokens=10,
        model_size=SimpleNamespace(value="small"))
    assert result == {"value": 2}
    args = gateway.structured.await_args.args
    assert args[0] == "small" and args[3] == [{"role": "user", "content": "x"}]
    assert args[4]["name"] == "Out" and args[4]["strict"] is True
    assert gateway.structured.await_args.kwargs["max_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ModelGatewayError("x"), ValueError("v")])
async def test_llm_maps_gateway_and_parse_failures(failure: Exception) -> None:
    """Gateway and parsing failures become a sanitized graph_model_unavailable."""
    gateway = SimpleNamespace(chat=AsyncMock(side_effect=failure))
    llm, _, _ = _make_clients(_context(), gateway)
    with pytest.raises(GraphOperationError, match="graph_model_unavailable"):
        await llm._generate_response([SimpleNamespace(role="user", content="x")])


@pytest.mark.asyncio
async def test_llm_rejects_non_json_completion() -> None:
    """A completion that is not JSON is reported as unavailable, never echoed."""
    gateway = SimpleNamespace(chat=AsyncMock(return_value=_completion("not json")))
    llm, _, _ = _make_clients(_context(), gateway)
    with pytest.raises(GraphOperationError, match="graph_model_unavailable"):
        await llm._generate_response([SimpleNamespace(role="user", content="x")])


@pytest.mark.asyncio
async def test_embedder_validates_order_dimensions_and_values() -> None:
    """Embeddings must be indexed in order, exactly sized, finite and non-zero."""
    good = {"data": [{"index": 0, "embedding": [1, 0, 0]}, {"index": 1, "embedding": [0, 2.5, 0]}]}
    gateway = SimpleNamespace(embed=AsyncMock())

    async def embed(*_a: Any, before_send: Any, **_k: Any) -> Any:
        """Authorize like the real gateway before answering with the queued payload."""
        await before_send()
        return gateway.embed.return_value

    gateway.embed.side_effect = embed
    gateway.embed.return_value = good
    context = _context()
    _, embedder, _ = _make_clients(context, gateway)
    assert await embedder.create_batch(["a", "b"]) == [[1.0, 0.0, 0.0], [0.0, 2.5, 0.0]]
    context.authorize.assert_awaited_with("embedding")
    cases = {
        "graph_embedding_response_invalid": {"data": [good["data"][0]]},
        "graph_embedding_order_invalid": {"data": [good["data"][1], good["data"][0]]},
        "graph_embedding_dimension_invalid": {"data": [{"index": 0, "embedding": [1, 0]}, good["data"][1]]},
        "graph_embedding_value_invalid": {"data": [{"index": 0, "embedding": [0, 0, 0]}, good["data"][1]]},
    }
    for code, payload in cases.items():
        gateway.embed.return_value = payload
        with pytest.raises(GraphOperationError, match=code):
            await embedder.create_batch(["a", "b"])
    gateway.embed.return_value = {"data": [{"index": 0, "embedding": [float("nan"), 1, 1]}, good["data"][1]]}
    with pytest.raises(GraphOperationError, match="graph_embedding_value_invalid"):
        await embedder.create_batch(["a", "b"])


@pytest.mark.asyncio
async def test_embedder_input_and_dimension_guards() -> None:
    """Non-string input, empty batches and unconfigured dimensions are refused before egress."""
    gateway = SimpleNamespace(embed=AsyncMock())
    _, embedder, _ = _make_clients(_context(), gateway)
    with pytest.raises(GraphOperationError, match="graph_embedding_input_unsupported"):
        await embedder.create(["tokens"])
    with pytest.raises(GraphOperationError, match="graph_embedding_input_invalid"):
        await embedder.create_batch([])
    _, unconfigured, _ = _make_clients(_context(dimensions=None), gateway)
    with pytest.raises(GraphOperationError, match="graph_embedding_dimension_unconfigured"):
        await unconfigured.create_batch(["a"])
    gateway.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_encoder_ranks_by_score_then_index() -> None:
    """Valid rows are returned by descending score with index as the tiebreak."""
    rows = {"results": [{"index": 1, "relevance_score": 0.5}, {"index": 0, "score": 0.5}, {"index": 2, "score": 0.9}]}
    gateway = SimpleNamespace(rerank=AsyncMock(return_value=rows))
    _, _, ranker = _make_clients(_context(), gateway)
    assert await ranker.rank("q", ["a", "b", "c"]) == [("c", 0.9), ("a", 0.5), ("b", 0.5)]


@pytest.mark.asyncio
async def test_cross_encoder_rejects_bad_rows_and_unconfigured() -> None:
    """Duplicate/out-of-range indexes, non-finite scores and a missing reranker are refused."""
    gateway = SimpleNamespace(rerank=AsyncMock())
    _, _, ranker = _make_clients(_context(), gateway)
    for rows in (
        [{"index": 0, "score": 1}, {"index": 0, "score": 2}],
        [{"index": 5, "score": 1}],
        [{"index": 0, "score": float("inf")}],
        [{"index": True, "score": 1}],
    ):
        gateway.rerank.return_value = {"results": rows}
        with pytest.raises(GraphOperationError, match="graph_reranking_response_invalid"):
            await ranker.rank("q", ["a", "b"])
    _, _, none_ranker = _make_clients(_context(reranking=False), gateway)
    with pytest.raises(GraphOperationError, match="graph_reranking_unavailable"):
        await none_ranker.rank("q", ["a"])


@pytest.mark.asyncio
async def test_model_call_budget_is_enforced() -> None:
    """More than MAX_MODEL_CALLS authorizations in one operation are refused."""
    from modules.knowledge.temporal.adapter import MAX_MODEL_CALLS, _authorize

    context = _context()
    context.model_calls[0] = MAX_MODEL_CALLS
    with pytest.raises(GraphOperationError, match="graph_model_call_budget_exhausted"):
        await _authorize(context, "reasoning")
