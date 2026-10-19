"""External visualization router: both adapters with mocked transports; failures never fall back or guess."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from core.model_gateway.client import CapabilityUnsupported, ModelGatewayError, PrivacyPolicyDenied
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from modules.chat.generative_ui import external_router as viz
from modules.chat.generative_ui.visualization_router import CATEGORY_ROUTES

POLICY = RequestPolicy(workspace_id=uuid4(), actor_user_id=1, membership_revision=1, gateway_identity="a" * 64,
                       configuration_revision=1)
MAPPING = ModelMapping(model="m", destination="remote")
WINDOW = [{"role": "user", "content": "plot revenue over time"}]


class FakeGateway:
    """Records the last call and returns/raises a canned outcome for structured() and tools()."""

    def __init__(self, outcome: Any) -> None:
        self.outcome, self.calls = outcome, []

    async def _answer(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def structured(self, *args: Any, **kwargs: Any) -> Any:
        return await self._answer("structured", *args, **kwargs)

    async def tools(self, *args: Any, **kwargs: Any) -> Any:
        return await self._answer("tools", *args, **kwargs)


def _content(category: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps({"category": category})}}]}


def _tool(category: str) -> dict[str, Any]:
    args = json.dumps({"category": category})
    return {"choices": [{"message": {"tool_calls": [{"function": {"name": "select_visualization", "arguments": args}}]}}]}


async def _llm(gateway: FakeGateway, **kw: Any) -> str:
    return await viz.classify_llm(gateway, MAPPING, POLICY, WINDOW, **kw)  # type: ignore[arg-type]


def test_thirteen_categories_map_to_renderers() -> None:
    assert len(viz.CATEGORIES) == 13 and set(viz.CRITERIA) == set(CATEGORY_ROUTES)
    assert viz.decision_for("part_to_whole", "external:llm")["kind"] == "pie_chart"
    assert viz.decision_for("text", "external:llm")["renderer"] == "text"
    with pytest.raises(viz.VisualizationRouterError):
        viz.decision_for("plan", "external:llm")


def test_window_is_bounded_and_excludes_system_and_tool_output() -> None:
    messages = [{"role": "system", "content": "SECRET"}, {"role": "tool", "content": "tool out"}]
    messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}" + "x" * 7000} for i in range(6)]
    window = viz.bounded_window(messages)
    assert [m["content"][0] for m in window] == ["2", "3", "4", "5"]
    assert all(len(m["content"]) == 6000 for m in window)
    assert "SECRET" not in json.dumps(window)


def test_flag_off_and_local_only_refuse() -> None:
    with pytest.raises(viz.VisualizationRouterError) as off:
        viz.ensure_allowed(allow_remote=False, local_only=False)
    assert (off.value.code, off.value.retryable) == ("consent_required", False)
    with pytest.raises(viz.VisualizationRouterError) as local:
        viz.ensure_allowed(allow_remote=True, local_only=True)
    assert local.value.code == "local_only_turn"
    viz.ensure_allowed(allow_remote=True, local_only=False)


@pytest.mark.asyncio
async def test_llm_json_schema_valid_and_openrouter_flag() -> None:
    gateway = FakeGateway(_content("line_chart"))
    assert await _llm(gateway, base_url="https://openrouter.ai/api/v1") == "line_chart"
    kwargs = gateway.calls[0][2]
    assert kwargs["extra_body"] == {"provider": {"require_parameters": True}}
    schema = gateway.calls[0][1][4]["schema"]
    assert schema["properties"]["category"]["enum"] == list(viz.CATEGORIES)
    plain = FakeGateway(_content("map"))
    await _llm(plain, base_url="https://api.openai.com/v1")
    assert plain.calls[0][2]["extra_body"] is None


@pytest.mark.asyncio
async def test_llm_forced_tool_sends_tool_choice() -> None:
    gateway = FakeGateway(_tool("flowchart"))
    assert await _llm(gateway, mode="forced_tool") == "flowchart"
    assert gateway.calls[0][0] == "tools"
    assert gateway.calls[0][2]["tool_choice"]["function"]["name"] == "select_visualization"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [_content("pie"), {"choices": []}, {"choices": [{"message": {"content": "nope"}}]}])
async def test_llm_invalid_enum_raises_never_guesses(response: dict[str, Any]) -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await _llm(FakeGateway(response))
    assert err.value.code == "invalid_category"


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "code", "retryable"), [
    (ModelGatewayError("Model gateway request timed out"), "timeout", True),
    (ModelGatewayError("Model gateway returned HTTP 401"), "auth_error", False),
    (ModelGatewayError("Model gateway returned HTTP 500"), "provider_error", True),
    (CapabilityUnsupported("rejected"), "mode_unsupported", False),
    (PrivacyPolicyDenied("denied"), "privacy_denied", False),
])
async def test_llm_gateway_failures_map_to_safe_codes(exc: Exception, code: str, retryable: bool) -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await _llm(FakeGateway(exc))
    assert (err.value.code, err.value.retryable) == (code, retryable)


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok(body: dict[str, Any]) -> httpx.MockTransport:
    return _transport(lambda request: httpx.Response(200, json=body))


@pytest.mark.asyncio
async def test_typesafe_valid_answer_and_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"answers": {"visualization": {
            "type": "choice", "choice": "bar_chart", "probabilities": {"bar_chart": 0.8, "table": 0.2}}}})

    category, probs = await viz.classify_typesafe("k-secret", WINDOW, _transport(handler))
    assert category == "bar_chart" and probs == {"bar_chart": 0.8, "table": 0.2}
    request = seen[0]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer k-secret"
    body = json.loads(request.content)
    question = body["questions"]["visualization"]
    assert body["model"] == "jev-latest" and body["state"]["conversation"] == WINDOW
    assert question["type"] == "choice" and set(question["criteria"]) == set(viz.CATEGORIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"type": "choice", "choice": "pie", "probabilities": {}},
    {"type": "choice", "choice": "map", "probabilities": {"map": 1.5}},
    {"type": "choice", "choice": "map", "probabilities": {"unknown": 0.5}},
    {"type": "choice", "choice": "map", "probabilities": {"map": True}},
    {"type": "text", "choice": "map"},
])
async def test_typesafe_invalid_answers_raise(answer: dict[str, Any]) -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await viz.classify_typesafe("k", WINDOW, _ok({"answers": {"visualization": answer}}))
    assert err.value.code == "invalid_category"


@pytest.mark.asyncio
async def test_typesafe_missing_answers_raises() -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await viz.classify_typesafe("k", WINDOW, _ok({"nothing": 1}))
    assert err.value.code == "invalid_category"


@pytest.mark.asyncio
async def test_typesafe_timeout_and_auth_and_server_error() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(viz.VisualizationRouterError) as slow:
        await viz.classify_typesafe("k", WINDOW, _transport(timeout))
    assert (slow.value.code, slow.value.retryable) == ("timeout", True)
    with pytest.raises(viz.VisualizationRouterError) as auth:
        await viz.classify_typesafe("k", WINDOW, _transport(lambda r: httpx.Response(401)))
    assert (auth.value.code, auth.value.retryable) == ("auth_error", False)
    with pytest.raises(viz.VisualizationRouterError) as server:
        await viz.classify_typesafe("k", WINDOW, _transport(lambda r: httpx.Response(503)))
    assert server.value.code == "provider_error"


@pytest.mark.asyncio
async def test_typesafe_redirect_is_not_followed() -> None:
    redirect = _transport(lambda r: httpx.Response(302, headers={"location": "https://evil.example/"}))
    with pytest.raises(viz.VisualizationRouterError) as err:
        await viz.classify_typesafe("k", WINDOW, redirect)
    assert err.value.code == "provider_error"
