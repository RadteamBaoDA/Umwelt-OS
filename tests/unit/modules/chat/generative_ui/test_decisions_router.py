"""OpenRouter Decisions API adapter and model discovery: real gateway, mocked HTTP transport, no live calls."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import ModelMapping, RequestPolicy
from core.workspaces.schemas import WorkspaceContext
from modules.chat.generative_ui import external_router as viz

WS = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WS, role="owner", membership_revision=3)
DEST = "remote:or"
POLICY = RequestPolicy(
    workspace_id=WS, actor_user_id=7, membership_revision=3, gateway_identity="a" * 64, configuration_revision=1,
    reasoning_allowed=True, permitted_destinations=frozenset({DEST}), reasoning_destinations=frozenset({DEST}))
MAPPING = ModelMapping(model="cloudflare/clef-flash", destination="remote")
WINDOW = [{"role": "user", "content": "plot revenue over time"}]


async def _noop() -> None:
    """Per-attempt fence stub."""


def _gateway(monkeypatch: pytest.MonkeyPatch, handler: Any, api_key: str = "or-key") -> ModelGateway:
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    redis.delete = AsyncMock()
    gw = ModelGateway(redis, "https://openrouter.ai/api/v1", api_key, DEST, timeout_seconds=5, scope=SCOPE,
                      gateway_identity="a" * 64, configuration_revision=1, before_send=_noop)
    monkeypatch.setattr(
        ModelGateway, "_http_client", lambda self, base: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return gw


def _answer(choice: Any, type_: str = "choice") -> httpx.Response:
    return httpx.Response(200, json={"id": "x", "answers": {"visualization": {"type": type_, "choice": choice}}})


@pytest.mark.asyncio
async def test_request_shape_and_valid_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer("line_chart")

    category = await viz.classify_decisions(_gateway(monkeypatch, handler), MAPPING, POLICY, WINDOW, probe=True)
    assert category == "line_chart"
    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "https://openrouter.ai/api/alpha/decisions")
    assert request.headers["authorization"] == "Bearer or-key"
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"} and body["model"] == "cloudflare/clef-flash"
    assert body["state"] == ["user: plot revenue over time"]
    question = body["questions"]["visualization"]
    assert question["type"] == "choice" and question["instructions"].startswith(viz.INSTRUCTIONS)
    assert question["criteria"] == dict.fromkeys(viz.CATEGORIES)


@pytest.mark.asyncio
async def test_state_is_bounded_to_newest_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _answer("text")

    window = [{"role": "user", "content": "A" * 20000 + "END"}]
    await viz.classify_decisions(_gateway(monkeypatch, handler), MAPPING, POLICY, window, probe=True)
    state = seen[0]["state"][0]
    assert len(state) == viz.DECISION_STATE_CHARS and state.endswith("END")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    _answer("pie"), _answer(None), _answer("table", "score"),
    httpx.Response(200, json={"answers": {}}),
])
async def test_invalid_answer_is_rejected_without_fallback(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await viz.classify_decisions(_gateway(monkeypatch, lambda r: response), MAPPING, POLICY, WINDOW, probe=True)
    assert err.value.code == "invalid_category"


@pytest.mark.asyncio
async def test_auth_failure_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(viz.VisualizationRouterError) as err:
        await viz.classify_decisions(_gateway(monkeypatch, lambda r: httpx.Response(401)), MAPPING, POLICY, WINDOW, probe=True)
    assert (err.value.code, err.value.retryable) == ("auth_error", False)


@pytest.mark.asyncio
async def test_missing_key_or_consent_denied_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return _answer("text")

    with pytest.raises(viz.VisualizationRouterError) as no_key:
        await viz.classify_decisions(_gateway(monkeypatch, handler, api_key=""), MAPPING, POLICY, WINDOW, probe=True)
    assert no_key.value.code == "privacy_denied"
    denied = POLICY.model_copy(update={"reasoning_allowed": False})
    with pytest.raises(viz.VisualizationRouterError) as no_consent:
        await viz.classify_decisions(_gateway(monkeypatch, handler), MAPPING, denied, WINDOW, probe=True)
    assert no_consent.value.code == "privacy_denied" and not sent
    with pytest.raises(viz.VisualizationRouterError) as local:
        viz.ensure_allowed(allow_remote=True, local_only=True)
    assert local.value.code == "local_only_turn"


@pytest.mark.asyncio
async def test_discovery_requests_decisions_filter_and_bounds_result(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    data = [{"id": f"vendor/m{i}", "architecture": {"input_modalities": ["text", "image", "bogus"]}} for i in range(300)]
    data += [{"id": "bad id!"}, {"id": 5}, "x", {"id": "vendor/m1"}]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": data})

    payload = await _gateway(monkeypatch, handler).discover_decision_models()
    assert seen[0].method == "GET" and seen[0].url.path == "/api/v1/models"
    assert seen[0].url.params["output_modalities"] == "decisions"
    models = viz.parse_decision_models(payload)
    assert len(models) == viz.MAX_DECISION_MODELS
    assert models[0] == {"id": "vendor/m0", "input_modalities": ["image", "text"]}


def test_parse_decision_models_skips_invalid_and_duplicate_entries() -> None:
    payload = {"data": [{"id": "a/b"}, {"id": "a/b"}, {"id": "../x"}, {"id": ""}, {"id": "ok:free", "architecture": []}]}
    assert viz.parse_decision_models(payload) == [
        {"id": "a/b", "input_modalities": []}, {"id": "ok:free", "input_modalities": []}]
    assert viz.parse_decision_models("nope") == []
