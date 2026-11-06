"""model_identity: model-evidence/embedding identity ignores web search, prompts and row revision; gateway_identity does not."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.model_gateway.schemas import AIExecutionConfig
from modules.settings import models
from tests.unit.modules.settings.test_providers import GW, WS, _config, _enc, _row

_PROVIDERS = {
    "main": {
        "kind": "custom",
        "label": "M",
        "base_url": GW,
        "key_ciphertext": _enc("k1"),
        "models": [{"id": "m1", "type": "llm"}, {"id": "e1", "type": "embedding"}],
    }
}
_DEFAULTS = {
    "chat": {"model": "m1", "version": "1", "destination": "remote", "provider_id": "main"}
}


def _base(**over: object) -> dict[str, object]:
    return {"providers": _PROVIDERS, "defaults": _DEFAULTS, **over}


async def _cfg(**over: object) -> AIExecutionConfig:
    return await _config(_row(**_base(**over)))


@pytest.mark.asyncio
async def test_unrelated_saves_keep_model_identity_but_rotate_gateway_identity() -> None:
    base = await _cfg()
    for change in (
        {"web_search_provider": "searxng", "web_search_endpoint": "https://s.example"},
        {"web_search_api_key_ciphertext": _enc("web-key")},
        {
            "web_search_provider": "searxng",
            "web_search_endpoint": "https://s.example",
            "privacy": {
                "allow_remote_web_search": True,
                "web_search_destinations": ["remote:s.example"],
            },
        },
        {"configuration_revision": 9},
    ):
        other = await _cfg(**change)
        assert other.model_identity == base.model_identity, change
        assert other.gateway_identity != base.gateway_identity, change


@pytest.mark.asyncio
async def test_model_relevant_changes_rotate_model_identity() -> None:
    base = await _cfg()
    other_key = {"main": {**_PROVIDERS["main"], "key_ciphertext": _enc("k2")}}
    other_url = {"main": {**_PROVIDERS["main"], "base_url": "https://other.example/v1"}}
    other_defaults = {"chat": {**_DEFAULTS["chat"], "model": "m2"}}
    consent = {"allow_remote_reasoning": True, "reasoning_destinations": [await _dest()]}
    for change in (
        {"providers": other_key},
        {"providers": other_url},
        {"defaults": other_defaults},
        {"privacy": consent},
    ):
        assert (await _cfg(**change)).model_identity != base.model_identity, change


async def _dest() -> str:
    return (await _cfg()).providers["main"].destination_id  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_throttled_evidence_upsert_is_guarded_and_keyed_by_capability() -> None:
    """Real-call recording upserts per (workspace, provider, model, capability) and only rewrites on change or staleness."""
    from sqlalchemy.dialects import postgresql

    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    base = await _cfg()
    await models.save_model_test_result(
        session,
        workspace_id=WS,
        provider_id="main",
        model_id="m1",
        result="supported",
        capability="tools",
        model_identity=base.model_identity,
        throttle=True,
    )
    sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (workspace_id, provider_id, model_id, capability) DO UPDATE" in sql
    assert "IS DISTINCT FROM" in sql and "interval '1 hour'" in sql
    # An explicit Test save is unguarded: always writes.
    await models.save_model_test_result(
        session, workspace_id=WS, provider_id="main", model_id="m1", result="failed"
    )
    assert "IS DISTINCT FROM" not in str(
        session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    )
