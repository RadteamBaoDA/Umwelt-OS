"""Opt-in external visualization router: classify a conversation into one of 13 categories.

Two adapters, both server-validated and both failing closed: any invalid, timed-out or rejected answer raises
``VisualizationRouterError`` for the turn to surface as a retryable error. There is deliberately no guess and no
fallback to the internal router once an external router is selected.

Privacy: ``ensure_allowed`` refuses to run unless the owner granted ``allow_remote_visualization_routing`` and the
turn is not ``local_only``. Input is bounded (last 4 user/assistant messages, 6000 chars each); system prompts,
tool output and keys never enter the window. Keys are only ever sent in a header and never logged.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.model_gateway.transport import EndpointNetworkPolicyError
from modules.chat.generative_ui.visualization_router import CATEGORY_ROUTES

CATEGORIES: tuple[str, ...] = tuple(CATEGORY_ROUTES)
WINDOW_MESSAGES = 4
WINDOW_CHARS = 6000
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 64 * 1024
ROUTER_ALIAS = "viz-router"
_TOOL_NAME = "select_visualization"

INSTRUCTIONS = (
    "Pick the single best way to present the assistant's next reply to the user. "
    "Prefer 'text' unless a visual form clearly communicates the content better."
)
CRITERIA: dict[str, str] = {
    "text": "Plain prose answer, explanation, conversation or short fact; no visual needed.",
    "table": "Side-by-side comparison of several items across attributes, or a list of records.",
    "bar_chart": "Comparing or ranking magnitudes across categories.",
    "line_chart": "A trend or time series of one or more values over time.",
    "scatter_plot": "Relationship or correlation between two numeric variables.",
    "distribution": "Spread of values: histogram, box plot, frequency distribution.",
    "part_to_whole": "Proportions or shares of a total: pie or donut breakdown.",
    "flowchart": "A process, decision tree or workflow as connected steps.",
    "static_diagram": "A structural or architecture diagram that does not need interaction.",
    "interactive_diagram": "An explorable model or simulation with adjustable parameters.",
    "animated_route": "A multi-stop route or itinerary shown in sequence on a map.",
    "map": "Geographic locations plotted on a map.",
    "calculator": "An interactive calculator or estimator the user can fill in.",
}


class VisualizationRouterError(Exception):
    """Failure of the external router. ``code`` is a short safe string (never a URL, body or key).

    ``retryable`` tells the turn whether asking the user to retry can help (False for consent/policy denials).
    """

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        """Record the safe failure code and whether a retry may succeed."""
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def ensure_allowed(*, allow_remote: bool, local_only: bool) -> None:
    """Refuse external routing unless the owner opted in and the turn is not ``local_only``.

    Raises:
        VisualizationRouterError: ``local_only_turn`` or ``consent_required`` (both non-retryable).
    """
    if local_only:
        raise VisualizationRouterError("local_only_turn", retryable=False)
    if not allow_remote:
        raise VisualizationRouterError("consent_required", retryable=False)


def bounded_window(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Last 4 user/assistant text messages, each cut to 6000 chars, oldest first.

    System prompts, tool messages and non-string content are dropped so no secret or tool output leaves the host.
    """
    kept = [
        {"role": m["role"], "content": m["content"][:WINDOW_CHARS]}
        for m in messages
        if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    return kept[-WINDOW_MESSAGES:]


def _valid(category: Any) -> str:
    """Return ``category`` if it is one of the 13 enum values, otherwise raise ``invalid_category``."""
    if not isinstance(category, str) or category not in CATEGORY_ROUTES:
        raise VisualizationRouterError("invalid_category")
    return category


def decision_for(category: str, source: str) -> dict[str, Any]:
    """Router decision in the same shape as the internal ``route_visualization`` (renderer derived from category)."""
    renderer, kind = CATEGORY_ROUTES[_valid(category)]
    return {"renderer": renderer, "visualization": category, "kind": kind, "source": source}


def _is_openrouter(base_url: str | None) -> bool:
    """True when the provider host is openrouter.ai (needs ``provider.require_parameters`` for constrained output)."""
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _error_from_gateway(exc: Exception) -> VisualizationRouterError:
    """Map a gateway failure onto a safe router error code."""
    if isinstance(exc, PrivacyPolicyDenied):
        return VisualizationRouterError("privacy_denied", retryable=False)
    if isinstance(exc, CapabilityUnsupported):
        return VisualizationRouterError("mode_unsupported", retryable=False)
    text = str(exc)
    if "HTTP 401" in text or "HTTP 403" in text:
        return VisualizationRouterError("auth_error", retryable=False)
    if "timed out" in text:
        return VisualizationRouterError("timeout")
    return VisualizationRouterError("provider_error")


async def classify_llm(
    gateway: ModelGateway, mapping: ModelMapping, policy: RequestPolicy, window: list[dict[str, str]], *,
    mode: str = "json_schema", base_url: str | None = None, probe: bool = False,
    before_send: Callable[[], Awaitable[None]] | None = None, alias: str = ROUTER_ALIAS,
) -> str:
    """Adapter 1: classify through the configured model gateway with an enum-constrained answer.

    ``mode`` is ``json_schema`` (response_format) or ``forced_tool`` (forced tool_choice with an enum parameter).
    The answer is validated against the 13 categories server-side; anything else raises, nothing is guessed.
    ``probe`` skips the cached-capability check (used by Test routing, which then records the result).

    Raises:
        VisualizationRouterError: invalid answer, provider/timeout/auth failure, unsupported mode or denial.
    """
    messages = [{"role": "system", "content": INSTRUCTIONS + " Categories: " + json.dumps(CRITERIA)}, *window]
    extra = {"provider": {"require_parameters": True}} if _is_openrouter(base_url) else None
    schema = {"type": "object", "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
              "required": ["category"], "additionalProperties": False}
    try:
        if mode == "forced_tool":
            tool = {"type": "function", "function": {"name": _TOOL_NAME, "description": INSTRUCTIONS, "parameters": schema}}
            response = await gateway.tools(
                alias, mapping, policy, messages, [tool], probe, max_tokens=64, before_send=before_send,
                tool_choice={"type": "function", "function": {"name": _TOOL_NAME}}, extra_body=extra)
        elif mode == "json_schema":
            response = await gateway.structured(
                alias, mapping, policy, messages, {"name": "visualization_route", "strict": True, "schema": schema},
                probe, max_tokens=64, temperature=0, before_send=before_send, extra_body=extra)
        else:
            raise VisualizationRouterError("invalid_mode", retryable=False)
    except (ModelGatewayError, ValueError) as exc:
        raise _error_from_gateway(exc) from exc
    try:
        message = response["choices"][0]["message"]
        raw = message["tool_calls"][0]["function"]["arguments"] if mode == "forced_tool" else message["content"]
        return _valid(json.loads(raw)["category"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisualizationRouterError("invalid_category") from exc


async def classify_typesafe(
    api_key: str, window: list[dict[str, str]], transport: httpx.AsyncBaseTransport,
) -> tuple[str, dict[str, float]]:
    """Adapter 2: TypeSafe System One ``visualization`` choice question; returns (category, probabilities).

    ``transport`` must be the approved-endpoint transport (host pinned via ``approved_web_search_transport``): there is
    no stock-httpx path. 15 s timeout, no redirects, ``trust_env`` off, 64 KiB response cap. The key goes only in the
    Authorization header. Choice and probabilities (each within [0, 1]) are validated.

    Raises:
        VisualizationRouterError: timeout, auth (401/403), HTTP/transport error, network policy denial or bad answer.
    """
    body = {
        "model": "jev-latest",
        "state": {"conversation": window},
        "questions": {"visualization": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}},
    }
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TYPESAFE_TIMEOUT_SECONDS, follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.post(TYPESAFE_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException as exc:
        raise VisualizationRouterError("timeout") from exc
    except EndpointNetworkPolicyError as exc:
        raise VisualizationRouterError("network_denied", retryable=False) from exc
    except httpx.HTTPError as exc:
        raise VisualizationRouterError("provider_error") from exc
    if response.status_code in {401, 403}:
        raise VisualizationRouterError("auth_error", retryable=False)
    if not response.is_success or len(response.content) > MAX_RESPONSE_BYTES:
        raise VisualizationRouterError("provider_error")
    try:
        answer = response.json()["answers"]["visualization"]
        if answer["type"] != "choice":
            raise VisualizationRouterError("invalid_category")
        category = _valid(answer["choice"])
        raw = answer.get("probabilities") or {}
        probabilities = {
            _valid(name): float(value) for name, value in raw.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if len(probabilities) != len(raw) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities.values()):
            raise VisualizationRouterError("invalid_category")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VisualizationRouterError("invalid_category") from exc
    return category, probabilities
