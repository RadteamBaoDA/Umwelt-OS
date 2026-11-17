"""Provider-model capability probe: probe_model persists evidence in Postgres under the exact provider model."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelDefaults,
    ModelMapping,
    PrivacySettings,
    ProbeRequest,
    ProviderConfig,
    ProviderModel,
)
from core.workspaces.schemas import WorkspaceContext
from modules.model_gateway import routes
from modules.settings import public
from tests.unit.split_patching import patch_split_attr

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
IDENTITY = "b" * 64


def _config() -> AIExecutionConfig:
    """Snapshot whose chat default is NOT the model under test, served by a local provider."""
    return AIExecutionConfig(
        workspace_id=WS,
        actor_user_id=1,
        membership_revision=1,
        access_configuration_revision=1,
        configuration_revision=2,
        gateway_identity=IDENTITY,
        endpoint_destination_id="local:abc",
        gateway_base_url="http://ollama:11434",
        gateway_api_key="",
        gateway_credential_configured=False,
        defaults=ModelDefaults(
            chat=ModelMapping(
                model="other", version="v9", destination="unknown", provider_id="local"
            )
        ),
        privacy=PrivacySettings(),
        request_timeout_seconds=20,
        web_search_provider="none",
        web_search_endpoint=None,
        web_search_api_key="",
        providers={
            "local": ProviderConfig(
                kind="ollama",
                base_url="http://ollama:11434",
                destination_id="local:abc",
                local=True,
                models=(
                    ProviderModel(id="general-free"),
                    ProviderModel(id="embed-1", type="embedding"),
                ),
            )
        },
    )


class _Redis:
    """Minimal async key/value store standing in for Redis (breaker/lease state only; no capability evidence)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


async def _run(
    redis: _Redis, body: ProbeRequest, role: str = "chat", *, save: AsyncMock | None = None
) -> dict[str, object]:
    """Invoke probe_model with a stub gateway whose stream yields one content chunk; ``save`` stands in for the DB upsert."""

    async def stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "ready"}}]})
        yield "data: [DONE]"

    gateway = MagicMock()
    gateway.stream = stream
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(multi_workspace_enabled=False), redis=redis
            )
        )
    )
    session = MagicMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    with (
        patch_split_attr(
            "modules.settings", "get_ai_execution_config", AsyncMock(return_value=_config())
        ),
        patch_split_attr("modules.settings", "check_ai_execution_config", AsyncMock()),
        patch_split_attr("modules.settings", "save_capability", save or AsyncMock()),
        patch.object(routes, "ModelGateway", MagicMock(return_value=gateway)),
        patch.object(routes, "authenticated_session_ref", MagicMock()),
    ):
        return await routes.probe_model(request, session, SCOPE, role, body)  # type: ignore[arg-type]


def test_probe_request_requires_provider_and_model_together() -> None:
    with pytest.raises(ValueError):
        ProbeRequest(capability="streaming", provider_id="local")
    with pytest.raises(ValueError):
        ProbeRequest(capability="streaming", model="m")
    assert ProbeRequest(capability="streaming").model is None


@pytest.mark.asyncio
async def test_provider_model_probe_saves_evidence_chat_streaming_accepts() -> None:
    save = AsyncMock()
    result = await _run(
        _Redis(),
        ProbeRequest(capability="streaming", provider_id="local", model="general-free"),
        save=save,
    )
    assert result["result"] == "supported"
    save.assert_awaited_once()
    saved = save.await_args.args[1]
    assert (saved.model, saved.capability, saved.result) == (
        "general-free",
        "streaming",
        "supported",
    )
    assert save.await_args.kwargs["configured_mapping"] == public.mapping_for_override(
        _config(), "local", "general-free"
    )


@pytest.mark.asyncio
async def test_probe_evidence_mismatch_is_a_409_not_a_500() -> None:
    save = AsyncMock(side_effect=ValueError("Capability evidence does not match configured model"))
    with pytest.raises(HTTPException) as exc:
        await _run(
            _Redis(),
            ProbeRequest(capability="streaming", provider_id="local", model="general-free"),
            save=save,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_provider_model_probe_rejects_unknown_wrong_type_and_non_chat() -> None:
    redis = _Redis()
    for model, status in (("ghost", 404), ("embed-1", 409)):
        with pytest.raises(HTTPException) as exc:
            await _run(
                redis, ProbeRequest(capability="streaming", provider_id="local", model=model)
            )
        assert exc.value.status_code == status
    with pytest.raises(HTTPException) as exc:
        await _run(
            redis,
            ProbeRequest(capability="streaming", provider_id="local", model="general-free"),
            role="brief",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_save_capability_rejects_model_not_configured_on_provider() -> None:
    config = _config()
    bogus = ModelMapping(model="ghost", provider_id="local")
    result = public.new_capability_result(
        "chat",
        bogus,
        "streaming",
        "supported",
        config=config,
        scope=SCOPE,
        multi_workspace_enabled=False,
    )
    with pytest.raises(ValueError):
        await public.save_capability(
            MagicMock(),
            result,
            config=config,
            scope=SCOPE,
            multi_workspace_enabled=False,
            configured_mapping=bogus,
        )
