"""Anthropic native Messages API transport for ``ModelGateway`` (``kind == "anthropic"``).

The gateway keeps speaking OpenAI-shaped requests and responses to its callers. This module translates at the
boundary (pure functions, no I/O) and sends raw ``httpx`` through the gateway's endpoint-policy client, so the
privacy, lease, breaker, retry and CIDR-pinning fences stay identical to the OpenAI path. Provider failures are
re-raised as ``openai`` exception types so the gateway's existing ``except`` clauses apply unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from core.model_gateway.transport import EndpointNetworkPolicyError

# 2026-10-21 rejects temperature/top_p/top_k on every model (400), so the translation drops them.
ANTHROPIC_VERSION = "2026-10-21"
DEFAULT_MAX_TOKENS = 4096
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
_STOP_REASONS = {
    "end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop",
    "max_tokens": "length", "model_context_window_exceeded": "length",
    "tool_use": "tool_calls", "refusal": "content_filter",
}
_BAD_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
_MAX_REASONING_DETAILS = 64


def _finish_reason(stop_reason: Any) -> str:
    """Map an Anthropic ``stop_reason`` to an OpenAI ``finish_reason``; unknown values become ``stop``."""
    return _STOP_REASONS.get(stop_reason, "stop") if isinstance(stop_reason, str) else "stop"


def _tool_id(raw: Any) -> str:
    """Make a tool-call id satisfy Anthropic's ``^[a-zA-Z0-9_-]+$`` pattern.

    Ids minted by other providers earlier in a conversation may contain other characters. The same pure
    mapping is applied to ``tool_use.id`` and ``tool_result.tool_use_id`` so pairs stay matched.
    """
    return _BAD_ID_CHARS.sub("_", str(raw)) or "call"


def _usage(raw: Any) -> dict[str, int]:
    """OpenAI usage from Anthropic usage; cache reads/writes count toward prompt tokens. Missing fields are 0."""
    raw = raw if isinstance(raw, dict) else {}

    def count(key: str) -> int:
        """Non-negative int value of one usage field."""
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    prompt = count("input_tokens") + count("cache_read_input_tokens") + count("cache_creation_input_tokens")
    completion = count("output_tokens")
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _text_of(content: Any) -> str:
    """Flatten OpenAI message content (string or list of text parts) to a string; other parts are ignored."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                       and isinstance(p.get("text"), str))
    return ""


def _user_blocks(content: Any) -> str | list[dict[str, Any]]:
    """Translate a user message's content: text parts and ``image_url`` parts (data URL or http(s) URL)."""
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str):
                continue
            match = _DATA_URL.match(url)
            if match:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": match[1], "data": match[2]}})
            elif url.startswith(("http://", "https://")):
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Assistant turn as ordered blocks: provider thinking blocks verbatim, then text, then ``tool_use``.

    Thinking blocks must be echoed back unmodified and first, or Anthropic rejects the next tool round.
    """
    blocks: list[dict[str, Any]] = []
    details = message.get("reasoning_details")
    if isinstance(details, list):
        blocks.extend(d for d in details[:_MAX_REASONING_DETAILS] if isinstance(d, dict) and d.get("type") in _THINKING_TYPES)
    text = _text_of(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        blocks.append({"type": "tool_use", "id": _tool_id(call.get("id")), "name": function.get("name"),
                       "input": arguments if isinstance(arguments, dict) else {}})
    return blocks


def to_messages_request(body: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    """Translate an OpenAI-shaped chat request into an Anthropic Messages request. Pure; never mutates ``body``.

    System messages become the top-level ``system``; tool results become ``tool_result`` user blocks; sampling
    parameters, ``extra_body`` and ``stream_options`` are dropped (400 on the current API version). Forced
    ``tool_choice`` is degraded to auto because Claude 5.x answers 400 to ``any``/``tool``.
    """
    system: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        role = message.get("role")
        if role == "system":
            if text := _text_of(message.get("content")):
                system.append(text)
        elif role == "user":
            messages.append({"role": "user", "content": _user_blocks(message.get("content"))})
        elif role == "assistant":
            if blocks := _assistant_blocks(message):
                messages.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            content = message.get("content")
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": _tool_id(message.get("tool_call_id")),
                "content": content if isinstance(content, str) else json.dumps(content)}]})
    out: dict[str, Any] = {
        "model": body.get("model"), "messages": messages,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or DEFAULT_MAX_TOKENS,
    }
    if system:
        out["system"] = "\n\n".join(system)
    tools = body.get("tools")
    if tools:
        out["tools"] = [
            {"name": t["function"]["name"], **({"description": t["function"]["description"]} if t["function"].get("description") else {}),
             "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict)]
    # ponytail: forced tool_choice ("required"/function) is dropped because Claude 5.x returns 400; older models could
    # honour it, so add a per-model allowlist if a caller needs forcing.
    if body.get("tool_choice") == "none":
        out["tool_choice"] = {"type": "none"}
    output_config: dict[str, Any] = {}
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if isinstance(schema, dict):
            output_config["format"] = {"type": "json_schema", "schema": schema}
    effort = body.get("reasoning_effort")
    if effort in {"low", "medium", "high"}:
        output_config["effort"] = effort
        # Summarized so streamed reasoning stays visible in the UI.
        out["thinking"] = {"type": "adaptive", "display": "summarized"}
    if output_config:
        out["output_config"] = output_config
    if stream:
        out["stream"] = True
    return out


def from_messages_response(data: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages response into an OpenAI ``chat.completion`` dict. Pure.

    Thinking text goes to ``reasoning_content`` and the raw blocks to ``reasoning_details`` (needed verbatim
    for the next tool round).
    """
    texts: list[str] = []
    thoughts: list[str] = []
    details: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            texts.append(block.get("text") or "")
        elif kind == "tool_use":
            calls.append({"id": block.get("id"), "type": "function", "function": {
                "name": block.get("name"), "arguments": json.dumps(block.get("input") or {})}})
        elif kind in _THINKING_TYPES:
            details.append(block)
            if kind == "thinking" and block.get("thinking"):
                thoughts.append(block["thinking"])
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if calls:
        message["tool_calls"] = calls
    if thoughts:
        message["reasoning_content"] = "".join(thoughts)
    if details:
        message["reasoning_details"] = details[:_MAX_REASONING_DETAILS]
    return {
        "id": data.get("id"), "object": "chat.completion", "model": data.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(data.get("stop_reason"))}],
        "usage": _usage(data.get("usage")),
    }


class StreamTranslator:
    """Stateful Anthropic SSE event to OpenAI ``chat.completion.chunk`` translator (one instance per stream)."""

    def __init__(self) -> None:
        """Start with no message metadata, no open blocks and zero usage."""
        self._id: str | None = None
        self._model: str | None = None
        self._input: dict[str, Any] = {}
        self._output = 0
        self._tool_ordinals: dict[int, int] = {}
        self._thinking: dict[int, dict[str, Any]] = {}
        self._details: list[dict[str, Any]] = []

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        """Wrap one delta in the chunk envelope."""
        choice: dict[str, Any] = {"index": 0, "delta": delta}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return {"id": self._id, "object": "chat.completion.chunk", "model": self._model, "choices": [choice]}

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Consume one decoded SSE event and return the chunks it produces (possibly none).

        Raises:
            ModelGatewayError: on an ``error`` event (static message; the upstream text is not echoed).
        """
        kind = event.get("type")
        raw_index = event.get("index")
        index = raw_index if isinstance(raw_index, int) else -1
        if kind == "message_start":
            message = event.get("message") or {}
            self._id, self._model = message.get("id"), message.get("model")
            self._input = dict(message.get("usage") or {})
            return [self._chunk({"role": "assistant"})]
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                ordinal = len(self._tool_ordinals)
                self._tool_ordinals[index] = ordinal
                return [self._chunk({"tool_calls": [{"index": ordinal, "id": block.get("id"), "type": "function",
                                                     "function": {"name": block.get("name"), "arguments": ""}}]})]
            if block.get("type") == "thinking":
                self._thinking[index] = {"type": "thinking", "thinking": "", "signature": ""}
            elif block.get("type") == "redacted_thinking":
                self._details.append(block)
            return []
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return [self._chunk({"content": delta.get("text") or ""})]
            if dtype == "input_json_delta" and index in self._tool_ordinals:
                return [self._chunk({"tool_calls": [{"index": self._tool_ordinals[index],
                                                     "function": {"arguments": delta.get("partial_json") or ""}}]})]
            if dtype == "thinking_delta":
                text = delta.get("thinking") or ""
                if index in self._thinking:
                    self._thinking[index]["thinking"] += text
                return [self._chunk({"reasoning_content": text})]
            if dtype == "signature_delta" and index in self._thinking:
                self._thinking[index]["signature"] += delta.get("signature") or ""
            return []
        if kind == "content_block_stop":
            if index in self._thinking:
                self._details.append(self._thinking.pop(index))
            return []
        if kind == "message_delta":
            usage = event.get("usage") or {}
            self._output = usage.get("output_tokens", self._output) or 0
            if isinstance(usage.get("input_tokens"), int):
                self._input = {**self._input, **usage}
            delta = event.get("delta") or {}
            payload: dict[str, Any] = {"reasoning_details": self._details[:_MAX_REASONING_DETAILS]} if self._details else {}
            return [self._chunk(payload, _finish_reason(delta.get("stop_reason")))]
        if kind == "message_stop":
            usage = _usage({**self._input, "output_tokens": self._output})
            return [{"id": self._id, "object": "chat.completion.chunk", "model": self._model, "choices": [], "usage": usage}]
        if kind == "error":
            from core.model_gateway.client import (
                ModelGatewayError,  # client imports this module
            )

            error = event.get("error") or {}
            raise ModelGatewayError(f"Model gateway error ({error.get('type') or 'unknown'})")
        return []


def headers(api_key: str) -> dict[str, str]:
    """Request headers: ``x-api-key`` auth (never ``Authorization``) plus the pinned API version."""
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}


def _status_error(response: httpx.Response) -> APIStatusError:
    """Build the ``openai.APIStatusError`` the gateway's handlers expect, keeping status and ``retry-after``."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = f"{error.get('type') or 'error'}: {error.get('message') or ''}".strip()
    return APIStatusError(message, response=response, body=body)


async def _send(http: httpx.AsyncClient, request: httpx.Request, *, stream: bool) -> httpx.Response:
    """Send one request mapping httpx failures and non-2xx statuses to ``openai`` exception types.

    A policy denial from the pinned transport keeps ``EndpointNetworkPolicyError`` as ``__cause__`` so the
    gateway's ``_raise_if_policy_denied`` still recognises it.
    """
    try:
        response = await http.send(request, stream=stream)
    except httpx.TimeoutException as exc:
        raise APITimeoutError(request=request) from exc
    except (httpx.TransportError, EndpointNetworkPolicyError) as exc:
        raise APIConnectionError(message="connection failed", request=request) from exc
    if response.status_code >= 400:
        try:
            await response.aread()
        finally:
            await response.aclose()
        raise _status_error(response)
    return response


async def create_message(
    http: httpx.AsyncClient, base_url: str, api_key: str, body: dict[str, Any], *, timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """POST ``{base_url}/messages`` for an OpenAI-shaped ``body`` and return an OpenAI ``chat.completion`` dict.

    Raises ``openai`` APIStatusError/APITimeoutError/APIConnectionError for upstream failures.
    """
    request = http.build_request("POST", f"{base_url}/messages", headers=headers(api_key),
                                 json=to_messages_request(body, stream=False), timeout=timeout)
    response = await _send(http, request, stream=False)
    data = response.json()
    return from_messages_response(data if isinstance(data, dict) else {})


async def open_message_stream(
    http: httpx.AsyncClient, base_url: str, api_key: str, body: dict[str, Any], *, timeout: httpx.Timeout | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Open a streaming Messages call; a non-2xx status raises before this returns (body already read).

    The returned async generator yields OpenAI ``chat.completion.chunk`` dicts and always closes the response.
    """
    request = http.build_request("POST", f"{base_url}/messages", headers=headers(api_key),
                                 json=to_messages_request(body, stream=True), timeout=timeout)
    response = await _send(http, request, stream=True)

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        """Parse SSE ``data:`` lines, translate each event and map mid-stream transport failures."""
        translator = StreamTranslator()
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(event, dict):
                    for chunk in translator.feed(event):
                        yield chunk
        except httpx.TimeoutException as exc:
            raise APITimeoutError(request=request) from exc
        except (httpx.TransportError, EndpointNetworkPolicyError) as exc:
            raise APIConnectionError(message="connection failed", request=request) from exc
        finally:
            await response.aclose()

    return chunks()


async def list_models(
    http: httpx.AsyncClient, base_url: str, api_key: str, *, timeout: httpx.Timeout | None = None,
) -> list[str]:
    """GET ``{base_url}/models?limit=1000`` and return the non-empty string model ids."""
    request = http.build_request("GET", f"{base_url}/models", params={"limit": 1000}, headers=headers(api_key),
                                 timeout=timeout)
    response = await _send(http, request, stream=False)
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return [item["id"] for item in data or [] if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]
