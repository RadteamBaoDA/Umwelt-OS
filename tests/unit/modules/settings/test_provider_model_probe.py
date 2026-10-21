"""Provider-model capability probe: evidence saved by probe_model must satisfy the gateway's streaming check."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelDefaults,
    ModelMapping,
    PrivacySettings,
    ProbeRequest,
    ProviderConfig,
    ProviderModel,
    RequestPolicy,
)
from core.workspaces.schemas import WorkspaceContext
from modules.model_gateway import routes
from modules.settings import public

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
IDENTITY = "b" * 64


def _config() -> AIExecutionConfig:
    """Snapshot whose chat default is NOT the model under test, served by a local provider."""
    return AIExecutionConfig(
        workspace_id=WS, actor_user_id=1, membership_revision=1, access_configuration_revision=1, configuration_revision=2,
        gateway_identity=IDENTITY, endpoint_destination_id="local:abc", gateway_base_url="http://ollama:11434",
        gateway_api_key="", gateway_credential_configured=False,
        defaults=ModelDefaults(chat=ModelMapping(model="other", version="v9", destination="unknown", provider_id="local")),
        privacy=PrivacySettings(), request_timeout_seconds=20,
        web_search_provider="none", web_search_endpoint=None, web_search_api_key="",
        providers={"local": ProviderConfig(
            kind="ollama", base_url="http://ollama:11434", destination_id="local:abc", local=True,
            models=(ProviderModel(id="general-free"), ProviderModel(id="embed-1", type="embedding")))})


class _Redis:
    """Minimal async key/value store standing in for Redis."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


async def _run(redis: _Redis, body: ProbeRequest, role: str = "chat") -> dict[str, object]:
    """Invoke probe_model with a stub gateway whose stream yields one content chunk."""
    async def stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "ready"}}]})
        yield "data: [DONE]"

    gateway = MagicMock()
    gateway.stream = stream
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(multi_workspace_enabled=False), redis=redis)))
    session = MagicMock()
    session.rollback = AsyncMock()
    with patch.object(public, "get_ai_execution_config", AsyncMock(return_value=_config())), \
         patch.object(public, "check_ai_execution_config", AsyncMock()), \
         patch.object(routes, "ModelGateway", MagicMock(return_value=gateway)), \
         patch.object(routes, "authenticated_session_ref", MagicMock()):
        return await routes.probe_model(request, session, SCOPE, role, body)  # type: ignore[arg-type]


def test_probe_request_requires_provider_and_model_together() -> None:
    with pytest.raises(ValueError):
        ProbeRequest(capability="streaming", provider_id="local")
    with pytest.raises(ValueError):
        ProbeRequest(capability="streaming", model="m")
    assert ProbeRequest(capability="streaming").model is None


@pytest.mark.asyncio
async def test_provider_model_probe_saves_evidence_chat_streaming_accepts() -> None:
    redis = _Redis()
    result = await _run(redis, ProbeRequest(capability="streaming", provider_id="local", model="general-free"))
    assert result["result"] == "supported" and len(redis.data) == 1

    # The chat worker builds the same mapping; the gateway's fail-closed check must now pass.
    config = _config()
    mapping = public.mapping_for_override(config, "local", "general-free")
    gateway = ModelGateway(redis, "http://ollama:11434", "", "local:abc", scope=SCOPE,  # type: ignore[arg-type]
                           gateway_identity=IDENTITY, configuration_revision=2, before_send=AsyncMock())
    policy = RequestPolicy(workspace_id=WS, actor_user_id=1, membership_revision=1, gateway_identity=IDENTITY, configuration_revision=2)
    assert await gateway._capability_supported("chat", mapping, policy, "streaming") is True
    other = ModelMapping(model="not-probed", provider_id="local")
    assert await gateway._capability_supported("chat", other, policy, "streaming") is False


@pytest.mark.asyncio
async def test_provider_model_probe_rejects_unknown_wrong_type_and_non_chat() -> None:
    redis = _Redis()
    for model, status in (("ghost", 404), ("embed-1", 409)):
        with pytest.raises(HTTPException) as exc:
            await _run(redis, ProbeRequest(capability="streaming", provider_id="local", model=model))
        assert exc.value.status_code == status
    with pytest.raises(HTTPException) as exc:
        await _run(redis, ProbeRequest(capability="streaming", provider_id="local", model="general-free"), role="brief")
    assert exc.value.status_code == 422
    assert redis.data == {}


@pytest.mark.asyncio
async def test_save_capability_rejects_model_not_configured_on_provider() -> None:
    config = _config()
    bogus = ModelMapping(model="ghost", provider_id="local")
    result = public.new_capability_result("chat", bogus, "streaming", "supported", config=config, scope=SCOPE, multi_workspace_enabled=False)
    with pytest.raises(ValueError):
        await public.save_capability(_Redis(), result, config=config, scope=SCOPE, multi_workspace_enabled=False, configured_mapping=bogus)  # type: ignore[arg-type]
