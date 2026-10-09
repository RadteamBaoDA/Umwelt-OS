"""read_ai_policy_fingerprint: nonsecret, changes with alias/endpoint/privacy."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.config import Settings
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, PrivacySettings
from modules.settings import public

WS = uuid4()
SECRET = "sk-super-secret"


def _config(**over: object) -> AIExecutionConfig:
    base: dict[str, object] = {
        "workspace_id": WS, "actor_user_id": 1, "membership_revision": 1, "access_configuration_revision": 1,
        "configuration_revision": 2, "gateway_identity": "a" * 64, "endpoint_destination_id": "omniroute:abc",
        "omniroute_base_url": "https://gw.example/v1", "omniroute_api_key": SECRET,
        "omniroute_credential_configured": True,
        "aliases": {"fast": ModelMapping(model="m1", version="1", destination="remote")},
        "privacy": PrivacySettings(allow_remote_reasoning=True, reasoning_destinations=["omniroute:abc"]),
        "chat_alias": "fast", "brief_alias": "fast", "request_timeout_seconds": 20,
        "web_search_provider": "none", "web_search_endpoint": None, "web_search_api_key": "",
    }
    return AIExecutionConfig(**{**base, **over})  # type: ignore[arg-type]


async def _fp(config: AIExecutionConfig | None, alias: str = "fast", owner: object = object()) -> str | None:
    with patch.object(public, "resolve_workspace_owner_context", AsyncMock(return_value=owner if config else None)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)):
        return await public.read_ai_policy_fingerprint(
            AsyncMock(), Settings(), None, workspace_id=WS, alias=alias)


@pytest.mark.asyncio
async def test_fingerprint_stable_nonsecret_and_sensitive() -> None:
    base = await _fp(_config())
    assert base and len(base) == 64 and SECRET not in base
    assert base == await _fp(_config())
    assert base != await _fp(_config(aliases={"fast": ModelMapping(model="m2", version="1", destination="remote")}))
    assert base != await _fp(_config(omniroute_base_url="https://other/v1", endpoint_destination_id="omniroute:zzz"))
    assert base != await _fp(_config(privacy=PrivacySettings(allow_remote_reasoning=True, reasoning_destinations=[])))
    assert base != await _fp(_config(configuration_revision=3))


@pytest.mark.asyncio
async def test_none_when_disabled_or_unconfigured() -> None:
    assert await _fp(None) is None
    assert await _fp(_config(privacy=PrivacySettings())) is None
    assert await _fp(_config(), alias="missing") is None
    assert await _fp(_config(omniroute_credential_configured=False)) is None
