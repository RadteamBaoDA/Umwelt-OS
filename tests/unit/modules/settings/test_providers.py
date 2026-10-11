"""Per-alias multi-provider AI settings: derivation, identity, save validation, consent and Redis degradation."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from redis.exceptions import RedisError

from core.config import Settings
from core.model_gateway.schemas import (
    AIExecutionConfig,
    AISettingsUpdate,
    ModelMapping,
    PrivacySettings,
    ProviderConfig,
    ProviderUpdate,
)
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.settings import models, public

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
KEY = Fernet.generate_key().decode()
GW = "https://gw.example/v1"
OLLAMA = "http://host.docker.internal:11434"


def _settings(public_ok: bool = False) -> MagicMock:
    settings = MagicMock(spec=Settings)
    settings.ai_allowed_endpoint_hosts = {"gw.example", "other.example", "host.docker.internal:11434"}
    settings.ai_allowed_endpoint_cidrs = []
    settings.ai_allow_public_providers = public_ok
    settings.ai_credential_encryption_key = SecretStr(KEY)
    settings.multi_workspace_enabled = False
    settings.omniroute_base_url = None
    settings.omniroute_api_key = SecretStr("")
    settings.omniroute_models = {}
    return settings


def _enc(value: str) -> str:
    return Fernet(KEY.encode()).encrypt(value.encode()).decode()


def _row(**over: object) -> SimpleNamespace:
    base = dict(
        configuration_revision=2, omniroute_base_url=None, omniroute_api_key_ciphertext=None, providers={}, aliases={},
        privacy={}, system_prompts={}, chat_alias="reasoning-large", brief_alias="reasoning-small",
        web_search_provider="none", web_search_endpoint=None, web_search_api_key_ciphertext=None, request_timeout_seconds=20)
    return SimpleNamespace(**{**base, **over})


async def _config(row: SimpleNamespace, settings: MagicMock | None = None) -> AIExecutionConfig:
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)):
        return await public.get_ai_execution_config(AsyncMock(), settings or _settings(), None, scope=SCOPE)


async def _save(row: SimpleNamespace, update: AISettingsUpdate, settings: MagicMock | None = None) -> SimpleNamespace:
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)), \
         patch.object(public, "read_ai_settings", AsyncMock()):
        await public.save_ai_settings(AsyncMock(), update, settings or _settings(), scope=SCOPE)
    return row


def _update(**over: object) -> AISettingsUpdate:
    return AISettingsUpdate(expected_revision=2, **over)  # type: ignore[arg-type]


def _provider(pid: str, kind: str = "openai", url: str = GW, **over: object) -> ProviderUpdate:
    return ProviderUpdate(id=pid, kind=kind, label=pid.title(), base_url=url, **over)  # type: ignore[arg-type]


# ---- derive_for_alias ------------------------------------------------------------------------------------------

def _derived_config() -> AIExecutionConfig:
    return AIExecutionConfig(
        workspace_id=WS, actor_user_id=1, membership_revision=1, access_configuration_revision=1, configuration_revision=2,
        gateway_identity="a" * 64, endpoint_destination_id="omniroute:default", omniroute_base_url="https://d.example",
        omniroute_api_key="default-key", omniroute_credential_configured=True,
        aliases={"fast": ModelMapping(model="a", destination="remote", provider_id="local1"),
                 "reasoning-large": ModelMapping(model="b", destination="remote"),
                 "reranker": ModelMapping(model="c", destination="remote", provider_id="ghost")},
        privacy=PrivacySettings(), chat_alias="fast", brief_alias="fast", request_timeout_seconds=20,
        web_search_provider="none", web_search_endpoint=None, web_search_api_key="",
        providers={
            "default": ProviderConfig(kind="omniroute", base_url="https://d.example", api_key="default-key",
                                      destination_id="omniroute:default", credential_configured=True),
            "local1": ProviderConfig(kind="ollama", base_url="http://ollama:11434", destination_id="local:abc", local=True)})


def test_derive_overrides_endpoint_key_and_destination_from_the_alias_provider() -> None:
    config = _derived_config()
    fast = public.derive_for_alias(config, "fast")
    assert (fast.omniroute_base_url, fast.omniroute_api_key, fast.endpoint_destination_id, fast.omniroute_credential_configured) == (
        "http://ollama:11434", "", "local:abc", False)
    default = public.derive_for_alias(config, "reasoning-large")
    assert (default.omniroute_api_key, default.endpoint_destination_id) == ("default-key", "omniroute:default")
    assert public.derive_for_alias(fast, "fast") == fast  # idempotent
    assert config.omniroute_base_url == "https://d.example"  # pure: input untouched


def test_derive_unknown_provider_is_unconfigured_and_providerless_config_passes_through() -> None:
    config = _derived_config()
    ghost = public.derive_for_alias(config, "reranker")
    assert ghost.omniroute_base_url is None and ghost.endpoint_destination_id is None and not ghost.omniroute_credential_configured
    bare = config.model_copy(update={"providers": {}})
    assert public.derive_for_alias(bare, "fast") is bare


def test_execution_config_never_dumps_or_reprs_provider_keys() -> None:
    config = _derived_config()
    assert "default-key" not in repr(config) and "default-key" not in config.model_dump_json()


# ---- identity / legacy synthesis --------------------------------------------------------------------------------

def test_identity_prefix_local_vs_remote() -> None:
    assert public._identity("http://x", "", local=True)[1].startswith("local:")  # type: ignore[union-attr]
    assert public._identity("http://x", "")[1].startswith("omniroute:")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_legacy_row_becomes_default_provider_with_the_same_destination_id() -> None:
    row = _row(omniroute_base_url=GW, omniroute_api_key_ciphertext=_enc("k1"))
    config = await _config(row)
    legacy_destination = f"omniroute:{public._fingerprint(GW)[:32]}"
    assert config.endpoint_destination_id == legacy_destination
    assert list(config.providers) == ["default"]
    default = config.providers["default"]
    assert (default.kind, default.destination_id, default.api_key, default.credential_configured) == (
        "omniroute", legacy_destination, "k1", True)
    read = public._read(config)
    assert read.providers[0].id == "default" and read.omniroute_credential_configured
    assert "k1" not in read.model_dump_json()


@pytest.mark.asyncio
async def test_gateway_identity_tracks_provider_key_and_url_but_not_label() -> None:
    def row(**rec: object) -> SimpleNamespace:
        base = {"kind": "openai", "label": "A", "base_url": GW, "key_ciphertext": _enc("k1")}
        return _row(providers={"p1": {**base, **rec}})

    base = (await _config(row())).gateway_identity
    assert base == (await _config(row())).gateway_identity
    assert base == (await _config(row(label="Renamed"))).gateway_identity
    assert base != (await _config(row(key_ciphertext=_enc("k2")))).gateway_identity
    assert base != (await _config(row(base_url="https://other.example/v1"))).gateway_identity


@pytest.mark.asyncio
async def test_local_flag_is_derived_from_kind_not_stored_value() -> None:
    config = await _config(_row(providers={"x": {"kind": "ollama", "label": "x", "base_url": OLLAMA, "local": False},
                                           "y": {"kind": "custom", "label": "y", "base_url": GW, "local": True}}))
    assert config.providers["x"].local and config.providers["x"].destination_id.startswith("local:")  # type: ignore[union-attr]
    assert not config.providers["y"].local and config.providers["y"].destination_id.startswith("omniroute:")  # type: ignore[union-attr]
    assert config.privacy.reasoning_destinations == []  # local providers never appear in consent lists


# ---- save validation --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    lambda: _update(providers=[_provider("a"), _provider("a")]),
    lambda: _update(providers=[_provider(f"p{i}") for i in range(9)]),
    lambda: _update(providers=[_provider("a")], aliases={"fast": ModelMapping(model="m", provider_id="nope")}),
    lambda: _update(providers=[_provider("a")], aliases={"local-private": ModelMapping(model="m", provider_id="a")}),
    lambda: _update(providers=[ProviderUpdate(id="a", kind="openai", label="a")]),  # no endpoint
    lambda: _update(providers=[_provider("a", url="https://evil.example/v1")]),  # not allowlisted
    lambda: _update(providers=[_provider("a", credential_action="replaced")]),  # replace without key
])
async def test_save_rejects_invalid_provider_sets(update) -> None:
    with pytest.raises(HTTPException) as exc:
        await _save(_row(), update())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_save_encrypts_keys_and_allows_local_private_only_on_local() -> None:
    row = await _save(_row(), _update(
        providers=[_provider("cloud", api_key="sk-secret", credential_action="replaced"), _provider("ol", "ollama", OLLAMA)],
        aliases={"local-private": ModelMapping(model="llama3", provider_id="ol")}))
    assert "sk-secret" not in str(row.providers)
    assert Fernet(KEY.encode()).decrypt(row.providers["cloud"]["key_ciphertext"].encode()).decode() == "sk-secret"
    assert row.providers["ol"]["local"] is True and row.providers["cloud"]["local"] is False
    assert row.omniroute_base_url is None  # no "default" provider -> legacy mirror cleared


@pytest.mark.asyncio
async def test_save_keeps_existing_key_when_unchanged_and_removes_on_request() -> None:
    ciphertext = _enc("old")
    row = _row(providers={"p": {"kind": "openai", "label": "P", "base_url": GW, "key_ciphertext": ciphertext}})
    await _save(row, _update(providers=[_provider("p")]))
    assert row.providers["p"]["key_ciphertext"] == ciphertext
    await _save(row, AISettingsUpdate(expected_revision=3, providers=[_provider("p", credential_action="removed")]))
    assert row.providers["p"]["key_ciphertext"] is None


@pytest.mark.asyncio
async def test_provider_endpoint_change_drops_stored_key() -> None:
    row = _row(providers={"p": {"kind": "openai", "label": "P", "base_url": GW, "key_ciphertext": _enc("old")}})
    await _save(row, _update(providers=[_provider("p", url="https://other.example/v1")]))
    assert row.providers["p"]["key_ciphertext"] is None


@pytest.mark.asyncio
async def test_absent_providers_keeps_them_and_legacy_fields_edit_default_only() -> None:
    other = {"kind": "ollama", "label": "O", "base_url": OLLAMA, "key_ciphertext": None, "local": True}
    ciphertext = _enc("k")
    row = _row(providers={"default": {"kind": "omniroute", "label": "Default", "base_url": GW, "key_ciphertext": ciphertext},
                          "o": other})
    await _save(row, _update(omniroute_base_url="https://other.example/v1"))
    assert row.providers["o"] == other
    assert row.providers["default"]["base_url"] == "https://other.example/v1"
    # endpoint changed -> saved key must not follow it to the new host
    assert row.omniroute_base_url == "https://other.example/v1" and row.omniroute_api_key_ciphertext is None


@pytest.mark.asyncio
async def test_legacy_save_without_providers_creates_default() -> None:
    row = await _save(_row(), _update(omniroute_base_url=GW, omniroute_credential_action="replaced", omniroute_api_key="k"))
    assert list(row.providers) == ["default"] and row.providers["default"]["kind"] == "omniroute"


# ---- consent ----------------------------------------------------------------------------------------------------

def _dest(url: str, local: bool = False) -> str:
    return public._identity(url, "", local=local)[1]  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_new_provider_never_inherits_consent_but_existing_unchanged_one_keeps_it() -> None:
    existing = {"kind": "openai", "label": "E", "base_url": GW, "key_ciphertext": None}
    row = _row(providers={"e": existing})
    privacy = PrivacySettings(allow_remote_reasoning=True,
                              reasoning_destinations=[_dest(GW), _dest("https://other.example/v1")])
    await _save(row, _update(providers=[_provider("e"), _provider("new", url="https://other.example/v1")], privacy=privacy))
    assert row.privacy["reasoning_destinations"] == [_dest(GW)]
    assert row.privacy["allow_remote_reasoning"] is True


@pytest.mark.asyncio
async def test_changed_endpoint_and_removed_provider_lose_consent() -> None:
    row = _row(providers={"e": {"kind": "openai", "label": "E", "base_url": GW, "key_ciphertext": None}})
    privacy = PrivacySettings(allow_remote_reasoning=True, reasoning_destinations=[_dest(GW), _dest("https://other.example/v1")])
    await _save(row, _update(providers=[_provider("e", url="https://other.example/v1")], privacy=privacy))
    assert row.privacy["reasoning_destinations"] == [] and row.privacy["allow_remote_reasoning"] is False
    row = _row(providers={"e": {"kind": "openai", "label": "E", "base_url": GW, "key_ciphertext": None}},
               privacy={"allow_remote_reasoning": True, "reasoning_destinations": [_dest(GW)]})
    await _save(row, _update(providers=[], privacy=PrivacySettings(allow_remote_reasoning=True, reasoning_destinations=[_dest(GW)])))
    assert row.privacy["reasoning_destinations"] == [] and row.providers == {}


@pytest.mark.asyncio
async def test_legacy_toggle_still_grants_default_and_local_never_gets_a_grant() -> None:
    row = _row(omniroute_base_url=GW, providers={"default": {"kind": "omniroute", "label": "D", "base_url": GW, "key_ciphertext": None},
                                                 "ol": {"kind": "ollama", "label": "O", "base_url": OLLAMA, "key_ciphertext": None}})
    await _save(row, _update(omniroute_base_url=GW, privacy=PrivacySettings(allow_remote_reasoning=True)))
    assert row.privacy["reasoning_destinations"] == [_dest(GW)]


# ---- endpoint policy --------------------------------------------------------------------------------------------

def test_public_providers_flag_admits_only_https_hostnames() -> None:
    off, on = _settings(False), _settings(True)
    with pytest.raises(HTTPException):
        public._endpoint("https://api.openai.com/v1", off)
    assert public._endpoint("https://api.openai.com/v1", on) == "https://api.openai.com/v1"
    for blocked in ("http://api.openai.com/v1", "https://8.8.8.8/v1"):
        with pytest.raises(HTTPException):
            public._endpoint(blocked, on)


# ---- draft credential reuse -------------------------------------------------------------------------------------

def test_draft_reuses_stored_key_only_for_the_same_endpoint() -> None:
    config = _derived_config()
    key, local, destination = public.resolve_draft_provider(config, "https://d.example", "", None, None)
    assert (key, local) == ("default-key", False) and destination.startswith("omniroute:")
    assert public.resolve_draft_provider(config, "https://evil.example", "", None, None)[0] == ""
    assert public.resolve_draft_provider(config, "https://d.example", "typed", None, None)[0] == "typed"
    key, local, destination = public.resolve_draft_provider(config, "http://x:11434", "", None, "ollama")
    assert (key, local) == ("", True) and destination.startswith("local:")


# ---- Redis degradation ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_aliases_degrades_to_defaults_when_redis_is_down() -> None:
    settings = _settings()
    settings.omniroute_models = {"fast": "m-fast"}
    redis = MagicMock()
    redis.hgetall = AsyncMock(side_effect=RedisError("down"))
    aliases = await models.legacy_aliases(redis, settings, scope=SCOPE)
    assert aliases["fast"].model == "m-fast"


@pytest.mark.asyncio
async def test_list_capabilities_returns_empty_when_redis_is_down() -> None:
    config = _derived_config()
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=RedisError("down"))
    assert await models.list_capabilities(redis, config=config, scope=SCOPE, multi_workspace_enabled=False) == []


# ---- every ModelGateway(...) construction is fed from a derived config ----------------------------------------------

def test_every_model_gateway_construction_uses_a_derived_config() -> None:
    root = Path(__file__).resolve().parents[4] / "modules"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?<![A-Za-z_])ModelGateway\(", text) and not re.search(r"derive_for_alias|resolve_draft_provider", text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"ModelGateway built without derive_for_alias: {offenders}"
