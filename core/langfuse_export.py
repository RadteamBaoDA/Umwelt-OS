"""Optional, metadata-only OpenTelemetry export to a Langfuse endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger("bbd.telemetry.langfuse")
_QUEUE_LIMIT = 32
_SEND_TIMEOUT_SECONDS = 2.0
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
_UNSAFE_MODEL = re.compile(r"(?i)(://|@|\?|=|bearer|token|secret|password|(?:sk|rk|pk)-[a-z0-9_-]{16,}|"
                           r"(?:ghp|gho|ghu|ghs|ghr)_[a-z0-9]{20,}|github_pat_|xox[abprs]-|AIza|eyJ)")
_queue: asyncio.Queue[dict[str, Any]] | None = None
_queue_loop: asyncio.AbstractEventLoop | None = None
_worker: asyncio.Task[None] | None = None


def _configuration() -> tuple[str, str, str] | None:
    """Return a validated HTTPS endpoint and backend credentials only after explicit egress approval."""
    if os.getenv("BBD_LANGFUSE_ENABLED", "false").casefold() != "true":
        return None
    if os.getenv("BBD_TELEMETRY_EGRESS_APPROVED", "false").casefold() != "true":
        return None

    base_url = os.getenv("BBD_LANGFUSE_BASE_URL", "").strip()
    public_key = os.getenv("BBD_LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("BBD_LANGFUSE_SECRET_KEY", "").strip()
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
                or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            return None
    except ValueError:
        return None
    if not public_key or not secret_key:
        return None
    return base_url.rstrip("/") + "/api/public/otel/v1/traces", public_key, secret_key


def _attribute(key: str, value: str | int) -> dict[str, Any]:
    """Format one scalar attribute using the OpenTelemetry JSON wire representation."""
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(
    *, capability: str, model_identity: str | None, ok: bool, usage: dict[str, Any],
    trace: dict[str, str | None], started: float,
) -> dict[str, Any]:
    """Build a content-free OTLP JSON span with stable request/run correlation and nullable usage."""
    safe_capability = _SAFE_NAME.sub("_", capability)[:64] or "unknown"
    raw_model = model_identity or ""
    safe_model = _SAFE_NAME.sub("_", raw_model)[:96] if not _UNSAFE_MODEL.search(raw_model) else ""
    correlation_keys = ("request_id", "agent_run_id", "ingestion_run_id", "tool_call_id")
    correlation_id = next((trace.get(key) for key in correlation_keys if trace.get(key)), None)
    try:
        trace_id = UUID(str(correlation_id)).hex if correlation_id else uuid4().hex
    except ValueError:
        trace_id = uuid4().hex

    attrs = [
        _attribute("langfuse.observation.type", "generation"),
        _attribute("langfuse.observation.name", f"model.{safe_capability}"),
        _attribute("langfuse.trace.name", "bbd.model_call"),
    ]
    for field, label in (("request_id", "request_id"), ("agent_run_id", "agent_run_id"),
                         ("ingestion_run_id", "ingestion_run_id"), ("tool_call_id", "tool_call_id")):
        value = trace.get(field)
        try:
            normalized = str(UUID(value)) if value else None
        except ValueError:
            normalized = None
        if normalized:
            attrs.append(_attribute(f"langfuse.trace.metadata.{label}", normalized))
    if safe_model:
        attrs.append(_attribute("gen_ai.request.model", safe_model))
        attrs.append(_attribute("gen_ai.response.model", safe_model))
    if isinstance(usage.get("tokens_in"), int) and usage["tokens_in"] >= 0:
        attrs.append(_attribute("gen_ai.usage.input_tokens", usage["tokens_in"]))
    if isinstance(usage.get("tokens_out"), int) and usage["tokens_out"] >= 0:
        attrs.append(_attribute("gen_ai.usage.output_tokens", usage["tokens_out"]))

    end_ns = time.time_ns()
    elapsed_ns = max(0, int((time.perf_counter() - started) * 1_000_000_000))
    span = {
        "traceId": trace_id,
        "spanId": uuid4().hex[:16],
        "name": "model_call",
        "kind": 3,
        "startTimeUnixNano": str(max(0, end_ns - elapsed_ns)),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": 1 if ok else 2},
    }
    return {"resourceSpans": [{
        "resource": {"attributes": [_attribute("service.name", "bbd-os")]},
        "scopeSpans": [{"scope": {"name": "bbd-os.telemetry", "version": "1"}, "spans": [span]}],
    }]}


def submit_model_summary(
    *, capability: str, model_identity: str | None, ok: bool, usage: dict[str, Any],
    trace: dict[str, str | None], started: float,
) -> None:
    """Admit a scrubbed model summary to a bounded background queue without delaying its caller."""
    global _queue, _queue_loop, _worker
    config = _configuration()
    if config is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    queue = _queue
    if queue is None or _queue_loop is not loop:
        queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        _queue = queue
        _queue_loop = loop
        _worker = None
    try:
        queue.put_nowait({"config": config, "payload": _span(
            capability=capability, model_identity=model_identity, ok=ok, usage=usage,
            trace=trace, started=started,
        )})
    except asyncio.QueueFull:
        logger.debug("Langfuse metadata export dropped (queue full)")
        return
    if _worker is None or _worker.done():
        _worker = loop.create_task(_export_queue(queue))


async def _export_queue(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Send spans serially with a cooperative total deadline and no response-body buffering.

    The HTTPX timeout also bounds individual I/O operations. A total timeout covers client
    creation, sending, and response/client cleanup; failures only drop optional telemetry.
    """
    while True:
        item = await queue.get()
        endpoint, public_key, secret_key = item["config"]
        try:
            async with asyncio.timeout(_SEND_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=_SEND_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST", endpoint, json=item["payload"], auth=(public_key, secret_key),
                        headers={"x-langfuse-ingestion-version": "4"},
                    ) as response:
                        status_code = response.status_code
                    if not 200 <= status_code < 300:
                        logger.debug("Langfuse metadata export failed (http_status=%d)", status_code)
        except Exception as exc:  # noqa: BLE001 - an unavailable optional sink cannot alter application results
            logger.debug("Langfuse metadata export failed (%s)", type(exc).__name__)
        finally:
            queue.task_done()
