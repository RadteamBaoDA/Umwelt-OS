"""Per-send chat model override: schema pairing, route validation and run-context storage."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from core.model_gateway.schemas import ProviderConfig, ProviderModel
from modules.chat import routes
from modules.chat.schemas import SendMessageRequest
from modules.settings import public as settings_public


def _config(*, denied: bool = False) -> Any:
    provider = ProviderConfig(
        kind="custom", base_url="https://x.example", destination_id="d", policy_denied=denied,
        models=(ProviderModel(id="gpt-a"), ProviderModel(id="embed-a", type="embedding")),
    )
    return SimpleNamespace(providers={"p1": provider})


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None, redis=None)))


async def _validate(monkeypatch: pytest.MonkeyPatch, payload: SendMessageRequest, config: Any) -> Any:
    async def fake(*_a: Any, **_k: Any) -> Any:
        return config

    monkeypatch.setattr(settings_public, "get_ai_execution_config", fake)
    return await routes._validated_model_override(_request(), None, None, payload)  # type: ignore[arg-type]


def test_provider_and_model_must_come_together() -> None:
    assert SendMessageRequest(content="hi").model is None
    assert SendMessageRequest(content="hi", provider_id="p1", model="gpt-a").model == "gpt-a"
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hi", model="gpt-a")


@pytest.mark.asyncio
async def test_valid_override_default_and_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = SendMessageRequest(content="hi", provider_id="p1", model="gpt-a")
    assert await _validate(monkeypatch, ok, _config()) == {"provider_id": "p1", "model": "gpt-a"}
    assert await _validate(monkeypatch, SendMessageRequest(content="hi"), _config()) is None
    bad = [
        (SendMessageRequest(content="hi", provider_id="p1", model="nope"), _config()),
        (SendMessageRequest(content="hi", provider_id="p1", model="embed-a"), _config()),
        (SendMessageRequest(content="hi", provider_id="p9", model="gpt-a"), _config()),
        (ok, _config(denied=True)),
    ]
    for payload, config in bad:
        with pytest.raises(HTTPException) as info:
            await _validate(monkeypatch, payload, config)
        assert info.value.status_code == 422


def test_run_context_stores_override_only_when_given() -> None:
    privacy = SimpleNamespace(store_conversation_history=True, persisted=True, updated_at=datetime(2026, 10, 7, tzinfo=UTC))
    override = {"provider_id": "p1", "model": "gpt-a"}
    assert routes._run_context(None, privacy, False, override)["_model_override"] == override
    assert "_model_override" not in routes._run_context({"_model_override": override}, privacy, False)
