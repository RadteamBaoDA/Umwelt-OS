"""Wire-protocol strategies for the model gateway (OpenAI-compatible and Anthropic Messages).

``ModelGateway`` owns everything that is protocol independent (privacy fences, leases, circuit breaker, retry,
capability evidence, telemetry). A ``WireProtocol`` owns only how one request is encoded, sent and decoded
for a provider family, so a new vendor protocol is added by registering one object here without editing the
client. Protocols raise the ``openai`` exception types (``APIStatusError`` and friends) so the shared retry
handling stays identical for every protocol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from openai import AsyncOpenAI

from core.model_gateway import anthropic

CHAT_PATH = "chat/completions"


class UnsupportedWireOperation(Exception):  # internal signal translated to ModelGatewayError by the client
    """Raised by a protocol for an operation path it does not implement; the client maps it to a gateway error."""


@dataclass(frozen=True, slots=True)
class WireSession:
    """Per-call transport handles shared with a protocol: pinned http client, SDK client, endpoint and credentials."""

    http: httpx.AsyncClient
    client: AsyncOpenAI
    base_url: str
    api_key: str
    timeout: httpx.Timeout


class WireProtocol(Protocol):
    """Strategy for one provider wire format.

    Capability flags let callers branch on behaviour instead of on the provider ``kind`` string:
    ``echoes_reasoning_details`` (assistant ``reasoning_details`` must be echoed back verbatim on tool turns)
    and ``requires_max_tokens`` (the upstream mandates ``max_tokens`` and rejects an oversize value with a
    400 that is a parameter problem, not a capability verdict).
    """

    name: str
    display_name: str
    echoes_reasoning_details: bool
    requires_max_tokens: bool

    def supports_path(self, path: str) -> bool:
        """True when ``path`` (``chat/completions``, ``embeddings``, ``rerank`` ...) exists for this protocol."""
        ...

    async def create(self, session: WireSession, path: str, body: dict[str, Any]) -> Any:
        """Send one non-stream request and return the decoded OpenAI-shaped response (dict or SDK model)."""
        ...

    async def open_stream(self, session: WireSession, model: str, messages: list[dict[str, Any]],
                          extra: dict[str, Any]) -> Any:
        """Open a streaming chat call and return an async iterator of OpenAI-shaped chunks (dicts or SDK models)."""
        ...

    async def list_models(self, session: WireSession) -> list[str]:
        """Return the non-empty model ids the upstream advertises."""
        ...


class OpenAICompatProtocol:
    """OpenAI-compatible HTTP API (OpenAI, Groq, OpenRouter, Gemini-compat, local servers) via the OpenAI SDK."""

    name = "openai"
    display_name = "OpenAI-compatible provider"
    echoes_reasoning_details = False
    requires_max_tokens = False

    def supports_path(self, path: str) -> bool:
        """Every gateway path is attempted; unknown ones fail in ``create`` with ``UnsupportedWireOperation``."""
        return True

    async def create(self, session: WireSession, path: str, body: dict[str, Any]) -> Any:
        """Dispatch ``path`` to the matching SDK call; raises ``UnsupportedWireOperation`` for an unknown path."""
        client = session.client
        if path == CHAT_PATH:
            return await client.chat.completions.create(**body)
        if path == "embeddings":
            return await client.embeddings.create(**body)
        if path == "embeddings-native":
            # Cohere-style upstreams reject the OpenAI `input` field; same URL, native body.
            return await client.post("/embeddings", cast_to=dict[str, Any], body=body)
        if path == "rerank":
            return await client.post("/rerank", cast_to=dict[str, Any], body=body)
        if path == "decisions":
            # OpenRouter Decisions (alpha) lives beside /api/v1 at /api/alpha; same host, so the
            # pinned http client and endpoint policy already cover it.
            return await client.post(
                re.sub(r"/v1$", "/alpha", session.base_url) + "/decisions", cast_to=dict[str, Any], body=body)
        raise UnsupportedWireOperation(path)

    async def open_stream(self, session: WireSession, model: str, messages: list[dict[str, Any]],
                          extra: dict[str, Any]) -> Any:
        """Open an SDK chat stream; ``extra`` (tools, tool_choice, max_tokens, reasoning_effort) is forwarded as is."""
        return await session.client.chat.completions.create(
            model=model,
            messages=cast("Any", messages),  # OpenAI param TypedDicts; built by prompt layer
            stream=True,
            **extra,
        )

    async def list_models(self, session: WireSession) -> list[str]:
        """List ``/models`` through the SDK and keep non-empty string ids."""
        page = await session.client.models.list()
        return [item.id for item in page.data if isinstance(item.id, str) and item.id]


class AnthropicMessagesProtocol:
    """Anthropic native Messages API, translated to and from the OpenAI shape by ``core.model_gateway.anthropic``."""

    name = "anthropic"
    display_name = "Anthropic"
    echoes_reasoning_details = True
    requires_max_tokens = True

    def supports_path(self, path: str) -> bool:
        """Only chat exists: Anthropic has no embeddings, rerank or decisions API."""
        return path == CHAT_PATH

    async def create(self, session: WireSession, path: str, body: dict[str, Any]) -> Any:
        """POST the translated Messages request over the pinned http client (``path`` is already checked)."""
        return await anthropic.create_message(
            session.http, session.base_url, session.api_key, body, timeout=session.timeout)

    async def open_stream(self, session: WireSession, model: str, messages: list[dict[str, Any]],
                          extra: dict[str, Any]) -> Any:
        """Open the native Messages SSE stream, yielding OpenAI-shaped chunk dicts."""
        return await anthropic.open_message_stream(
            session.http, session.base_url, session.api_key,
            {"model": model, "messages": messages, **extra}, timeout=session.timeout)

    async def list_models(self, session: WireSession) -> list[str]:
        """GET the native ``/models`` listing with ``x-api-key`` auth."""
        return await anthropic.list_models(session.http, session.base_url, session.api_key, timeout=session.timeout)


_DEFAULT_PROTOCOL: WireProtocol = OpenAICompatProtocol()
_REGISTRY: dict[str, WireProtocol] = {"anthropic": AnthropicMessagesProtocol()}


def register_wire_protocol(kind: str, protocol: WireProtocol) -> None:
    """Register ``protocol`` for provider ``kind`` so new vendors need no client edit; replaces an existing entry."""
    _REGISTRY[kind] = protocol


def wire_protocol_for(kind: str) -> WireProtocol:
    """Return the protocol registered for ``kind``; every unregistered kind is OpenAI-compatible."""
    return _REGISTRY.get(kind, _DEFAULT_PROTOCOL)
