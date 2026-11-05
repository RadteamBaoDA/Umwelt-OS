"""Gateway-backed Graphiti client adapters for the temporal graph adapter."""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Literal

from core.model_gateway.client import ModelGateway, ModelGatewayError
from modules.knowledge.temporal._types import (
    MAX_CANDIDATES,
    MAX_INPUT_BYTES,
    MAX_MODEL_CALLS,
    GraphOperationError,
    OperationAuthorization,
)

logger = logging.getLogger(__name__)


async def _gateway_generate(
    context: OperationAuthorization, gateway: ModelGateway, messages: list[Any],
    response_model: type[Any] | None, max_tokens: int, model_size: Any,
) -> dict[str, Any]:
    """Run one Graphiti generation through the gateway and return the validated JSON object.

    Selects the small or main reasoning policy, caps output at 4096 tokens, and
    reauthorizes (consuming the model-call budget) before every provider attempt.
    Gateway, parsing and schema failures become a sanitized ``graph_model_unavailable``
    so raw model output never reaches errors or logs.
    """
    payload = [{"role": item.role, "content": item.content} for item in messages]
    model = (
        context.small_reasoning
        if getattr(model_size, "value", "medium") == "small"
        else context.reasoning
    )
    token_limit = min(max_tokens or 4096, 4096)

    async def before_send() -> None:
        """Revalidate exact support and reasoning consent before each provider attempt."""
        await _authorize(context, "reasoning")

    try:
        if response_model is not None:
            schema = {
                "name": response_model.__name__, "strict": True,
                "schema": response_model.model_json_schema(),
            }
            raw = await gateway.structured(
                model.role, model.mapping,
                model.policy, payload, schema, max_tokens=token_limit,
                temperature=0, before_send=before_send,
            )
            content = _completion_content(raw)
            parsed = json.loads(content)
        else:
            raw = await gateway.chat(
                model.role, model.mapping,
                model.policy, payload, max_tokens=token_limit,
                temperature=0, before_send=before_send,
            )
            parsed = _completion_content(raw)
            parsed = json.loads(parsed)
        if response_model is not None:
            response_model.model_validate(parsed)
        parsed_result: dict[str, Any] = parsed
        return parsed_result
    except (ModelGatewayError, ValueError, TypeError, json.JSONDecodeError):
        raise GraphOperationError("graph_model_unavailable") from None


def _validated_embeddings(raw: Any, input_count: int, dimensions: int) -> list[list[float]]:
    """Validate ordered indexed vectors, exact dimensions, finite values and non-zero norm."""
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list) or len(data) != input_count:
        raise GraphOperationError("graph_embedding_response_invalid")
    indexes = [item.get("index") for item in data if isinstance(item, dict)]
    if indexes != list(range(len(data))):
        raise GraphOperationError("graph_embedding_order_invalid")
    vectors = [item.get("embedding") for item in data]
    if any(not isinstance(vector, list) or len(vector) != dimensions for vector in vectors):
        raise GraphOperationError("graph_embedding_dimension_invalid")
    result = [[float(value) for value in vector] for vector in vectors]
    if any(not math.isfinite(value) for vector in result for value in vector) or any(
        not any(value != 0 for value in vector) for vector in result
    ):
        raise GraphOperationError("graph_embedding_value_invalid")
    return result


async def _gateway_embed(
    context: OperationAuthorization, gateway: ModelGateway, input_data_list: list[str],
) -> list[list[float]]:
    """Embed a bounded batch through the gateway, requiring the configured dimensions and valid vectors."""
    model = context.embedding
    dimensions = model.dimensions
    if dimensions is None or not 1 <= dimensions <= 4096:
        raise GraphOperationError("graph_embedding_dimension_unconfigured")
    if not 1 <= len(input_data_list) <= MAX_CANDIDATES or any(
        not isinstance(item, str) or len(item.encode()) > MAX_INPUT_BYTES for item in input_data_list
    ):
        raise GraphOperationError("graph_embedding_input_invalid")

    async def before_send() -> None:
        """Revalidate the held source/document support and embedding grant per attempt."""
        await _authorize(context, "embedding")

    raw = await gateway.embed(
        model.role, model.mapping, model.policy, input_data_list, before_send=before_send,
    )
    return _validated_embeddings(raw, len(input_data_list), dimensions)


def _ranked_passages(rows: Any, passages: list[str]) -> list[tuple[str, float]]:
    """Validate unique in-range passage indexes with finite scores; order by score desc then index."""
    if not isinstance(rows, list) or len(rows) > len(passages):
        raise GraphOperationError("graph_reranking_response_invalid")
    ranked: list[tuple[int, str, float]] = []
    seen: set[int] = set()
    for row in rows:
        index = row.get("index") if isinstance(row, dict) else None
        score = row.get("relevance_score", row.get("score")) if isinstance(row, dict) else None
        if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(passages)
                or index in seen or not isinstance(score, int | float) or not math.isfinite(score)):
            raise GraphOperationError("graph_reranking_response_invalid")
        seen.add(index)
        ranked.append((index, passages[index], float(score)))
    return [(text, score) for _, text, score in sorted(ranked, key=lambda item: (-item[2], item[0]))]


async def _gateway_rerank(
    context: OperationAuthorization, gateway: ModelGateway, query: str, passages: list[str],
) -> list[tuple[str, float]]:
    """Rerank bounded passages through the explicitly configured gateway reranking capability."""
    model = context.reranking
    if model is None or not passages or len(passages) > MAX_CANDIDATES:
        raise GraphOperationError("graph_reranking_unavailable")

    async def before_send() -> None:
        """Revalidate complete operation support and embedding consent per attempt."""
        await _authorize(context, "embedding")

    raw = await gateway.rerank(
        model.role, model.mapping, model.policy, query, passages, before_send=before_send,
    )
    rows = raw.get("results") if isinstance(raw, dict) else None
    return _ranked_passages(rows, passages)


def _make_clients(context: OperationAuthorization, gateway: ModelGateway) -> tuple[Any, Any, Any]:
    """Adapt Graphiti clients to existing policy-aware gateway methods.

    The returned (llm, embedder, cross_encoder) are thin Graphiti subclasses; all
    validation lives in the module-level ``_gateway_*`` helpers.
    """
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient

    class GatewayLLM(LLMClient):
        """Route all Graphiti generation attempts through the current gateway policy."""

        def __init__(self) -> None:
            """Disable Graphiti's disk cache and pin generation bounds for gateway-routed requests."""
            from graphiti_core.llm_client.config import LLMConfig

            super().__init__(config=LLMConfig(temperature=0, max_tokens=4096), cache=False)

        async def _generate_response(
            self, messages: list[Any], response_model: type[Any] | None = None,
            max_tokens: int = 4096, model_size: Any = None,
        ) -> dict[str, Any]:
            """Translate messages/schema while keeping raw model output out of errors and logs."""
            return await _gateway_generate(context, gateway, messages, response_model, max_tokens, model_size)

        async def _generate_response_with_retry(
            self, messages: list[Any], response_model: type[Any] | None = None,
            max_tokens: int = 4096, model_size: Any = None,
        ) -> dict[str, Any]:
            """Keep Graphiti prompt normalization while leaving retry ownership to ModelGateway."""
            return await self._generate_response(
                messages, response_model, max_tokens or self.max_tokens, model_size,
            )

    class GatewayEmbedder(EmbedderClient):
        """Use configured gateway embeddings with exact dimensions and input bounds."""

        async def create(self, input_data: Any) -> list[float]:
            """Embed bounded text and reject token iterables lacking stable source provenance."""
            if not isinstance(input_data, str) or len(input_data.encode()) > MAX_INPUT_BYTES:
                raise GraphOperationError("graph_embedding_input_unsupported")
            return (await self.create_batch([input_data]))[0]

        async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
            """Validate ordered indexed vectors, exact dimensions, finite values, and nonzero norm."""
            return await _gateway_embed(context, gateway, input_data_list)

    class GatewayCrossEncoder(CrossEncoderClient):
        """Use only an explicitly configured gateway reranking capability."""

        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            """Validate unique bounded passage indexes and return deterministic score order."""
            return await _gateway_rerank(context, gateway, query, passages)

    return GatewayLLM(), GatewayEmbedder(), GatewayCrossEncoder()


async def _authorize(context: OperationAuthorization, capability: Literal["reasoning", "embedding"]) -> None:
    """Enforce owner freshness and aggregate outbound model-call limits."""
    if context.model_calls is None:
        raise RuntimeError("invariant violated: context.model_calls must not be None")
    context.model_calls[0] += 1
    if context.model_calls[0] > MAX_MODEL_CALLS:
        raise GraphOperationError("graph_model_call_budget_exhausted")
    await context.authorize(capability)


def _completion_content(value: Any) -> str:
    """Extract bounded OpenAI-compatible completion text without exposing invalid output."""
    choices = value.get("choices") if isinstance(value, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content.encode()) > MAX_INPUT_BYTES:
        raise ValueError("invalid completion")
    return content
