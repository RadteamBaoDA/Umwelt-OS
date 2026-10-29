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


# ---- regenerate / edit: carry or override the per-chat model


def _mutation(**kw: Any) -> Any:
    from modules.chat.schemas import MessageMutationRequest

    return MessageMutationRequest(action="regenerate", base_content_hash="a" * 64, client_request_id="r1", **kw)


async def _mutation_override(monkeypatch: pytest.MonkeyPatch, payload: Any, carried: Any, config: Any) -> Any:
    async def fake(*_a: Any, **_k: Any) -> Any:
        return config

    monkeypatch.setattr(settings_public, "get_ai_execution_config", fake)
    context = {"_model_override": carried} if carried is not None else {"kept": 1}
    return await routes._mutation_model_override(_request(), None, None, payload, context)  # type: ignore[arg-type]


def test_mutation_request_requires_pair() -> None:
    assert _mutation(provider_id="p1", model="gpt-a").model == "gpt-a"
    with pytest.raises(ValidationError):
        _mutation(provider_id="p1")


@pytest.mark.asyncio
async def test_mutation_carries_original_model_and_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    carried = {"provider_id": "p1", "model": "gpt-a"}
    assert await _mutation_override(monkeypatch, _mutation(), carried, _config()) == carried
    assert await _mutation_override(monkeypatch, _mutation(), None, _config()) is None
    config = SimpleNamespace(providers={"p1": ProviderConfig(
        kind="custom", base_url="https://x.example", destination_id="d",
        models=(ProviderModel(id="gpt-a"), ProviderModel(id="gpt-b")),
    )})
    chosen = _mutation(provider_id="p1", model="gpt-b")
    assert await _mutation_override(monkeypatch, chosen, carried, config) == {"provider_id": "p1", "model": "gpt-b"}


@pytest.mark.asyncio
async def test_mutation_carried_model_no_longer_valid_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    carried = {"provider_id": "p1", "model": "gone"}
    with pytest.raises(HTTPException) as info:
        await _mutation_override(monkeypatch, _mutation(), carried, _config())
    assert info.value.status_code == 422


def test_run_context_stores_override_and_strips_stale_private_keys() -> None:
    privacy = SimpleNamespace(store_conversation_history=True, persisted=True, updated_at=None)
    override = {"provider_id": "p1", "model": "gpt-a"}
    context = routes._run_context({"_model_override": {"model": "old"}, "kept": 1}, privacy, False, override)
    assert context["_model_override"] == override and context["kept"] == 1
