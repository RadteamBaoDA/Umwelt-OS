"""Minimal OpenAI-compatible fake model server for the disposable Compose harness.

Run with `uvicorn fake_model:app`. Streaming output is `tok0 tok1 ...` (one token per SSE chunk).
Control per request with `?tokens=N&delay_ms=M` or, when the gateway base URL cannot carry a query,
with a `[fake:tokens=N,delay_ms=M]` marker anywhere in the last user message.
"""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MODEL = os.getenv("FAKE_MODEL_NAME", "fake-chat")
DEFAULT_TOKENS = int(os.getenv("FAKE_MODEL_TOKENS", "5"))
DEFAULT_DELAY_MS = int(os.getenv("FAKE_MODEL_DELAY_MS", "0"))
_MARKER = re.compile(r"\[fake:([^\]]*)\]")


def fake_text(tokens: int) -> str:
    """Return the exact text a stream of `tokens` chunks concatenates to."""
    return "".join(f"tok{i} " for i in range(tokens))


def _options(request: Request, body: dict) -> tuple[int, int]:
    tokens, delay = DEFAULT_TOKENS, DEFAULT_DELAY_MS
    last = next((m.get("content", "") for m in reversed(body.get("messages", []))
                 if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    found = _MARKER.search(last)
    marker = dict(p.split("=", 1) for p in found.group(1).split(",") if "=" in p) if found else {}
    tokens = int(request.query_params.get("tokens", marker.get("tokens", tokens)))
    delay = int(request.query_params.get("delay_ms", marker.get("delay_ms", delay)))
    return tokens, delay


async def models(_request: Request) -> Response:
    return JSONResponse({"object": "list", "data": [
        {"id": MODEL, "object": "model", "created": 0, "owned_by": "fake"},
        {"id": "fake-embed", "object": "model", "created": 0, "owned_by": "fake"},
    ]})


async def embeddings(request: Request) -> Response:
    body = await request.json()
    inputs = body.get("input", [])
    inputs = [inputs] if isinstance(inputs, str) else inputs
    return JSONResponse({"object": "list", "model": body.get("model", "fake-embed"),
        "data": [{"object": "embedding", "index": i, "embedding": [0.1] * 8} for i in range(len(inputs))],
        "usage": {"prompt_tokens": 1, "total_tokens": 1}})


def _chunk(content: str | None, finish: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    payload = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


async def chat_completions(request: Request) -> Response:
    body = await request.json()
    tokens, delay = _options(request, body)
    if not body.get("stream"):
        return JSONResponse({"id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": fake_text(tokens)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": tokens, "total_tokens": tokens + 1}})

    async def events() -> AsyncIterator[str]:
        for i in range(tokens):
            yield _chunk(f"tok{i} ")
            if delay:
                await asyncio.sleep(delay / 1000)
        yield _chunk(None, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


app = Starlette(routes=[
    Route("/v1/models", models),
    Route("/v1/embeddings", embeddings, methods=["POST"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
])
