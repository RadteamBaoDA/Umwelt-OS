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
    settings.ai_credential_encryption_key_secondary = SecretStr("")
    settings.multi_workspace_enabled = False
    return settings


def _enc(value: str) -> str:
    return Fernet(KEY.encode()).encrypt(value.encode()).decode()


def _row(**over: object) -> SimpleNamespace:
    base = dict(
        configuration_revision=2, providers={}, aliases={}, n8n={},
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
        gateway_identity="a" * 64, endpoint_destination_id="remote:default", gateway_base_url="https://d.example",
        gateway_api_key="default-key", gateway_credential_configured=True,
        aliases={"fast": ModelMapping(model="a", destination="remote", provider_id="local1"),
                 "reasoning-large": ModelMapping(model="b", destination="remote"),
                 "reranker": ModelMapping(model="c", destination="remote", provider_id="ghost")},
        privacy=PrivacySettings(), chat_alias="fast", brief_alias="fast", request_timeout_seconds=20,
        web_search_provider="none", web_search_endpoint=None, web_search_api_key="",
        providers={
            "default": ProviderConfig(kind="custom", base_url="https://d.example", api_key="default-key",
                                      destination_id="remote:default", credential_configured=True),
            "local1": ProviderConfig(kind="ollama", base_url="http://ollama:11434", destination_id="local:abc", local=True)})


def test_derive_overrides_endpoint_key_and_destination_from_the_alias_provider() -> None:
    config = _derived_config()
    fast = public.derive_for_alias(config, "fast")
    assert (fast.gateway_base_url, fast.gateway_api_key, fast.endpoint_destination_id, fast.gateway_credential_configured) == (
        "http://ollama:11434", "", "local:abc", False)
    default = public.derive_for_alias(config, "reasoning-large")
    assert (default.gateway_api_key, default.endpoint_destination_id) == ("default-key", "remote:default")
    assert public.derive_for_alias(fast, "fast") == fast  # idempotent
    assert config.gateway_base_url == "https://d.example"  # pure: input untouched


def test_derive_unknown_provider_is_unconfigured_and_providerless_config_passes_through() -> None:
    config = _derived_config()
    ghost = public.derive_for_alias(config, "reranker")
    assert ghost.gateway_base_url is None and ghost.endpoint_destination_id is None and not ghost.gateway_credential_configured
    bare = config.model_copy(update={"providers": {}})
    assert public.derive_for_alias(bare, "fast") is bare


def test_execution_config_never_dumps_or_reprs_provider_keys() -> None:
    config = _derived_config()
    assert "default-key" not in repr(config) and "default-key" not in config.model_dump_json()


# ---- identity / legacy synthesis --------------------------------------------------------------------------------

def test_identity_prefix_local_vs_remote() -> None:
    assert public._identity("http://x", "", local=True)[1].startswith("local:")  # type: ignore[union-attr]
    assert public._identity("http://x", "")[1].startswith("remote:")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_first_provider_backs_the_top_level_mirror_when_no_default_exists() -> None:
    config = await _config(_row(providers={"main": {"kind": "custom", "label": "M", "base_url": GW, "key_ciphertext": _enc("k1"),
                                                    "models": [{"id": "m1", "type": "embedding"}]}}))
    main = config.providers["main"]
    assert (config.gateway_base_url, config.gateway_api_key) == (GW, "k1")
    assert main.destination_id.startswith("remote:") and [(m.id, m.type) for m in main.models] == [("m1", "embedding")]  # type: ignore[union-attr]
    derived = public.derive_for_alias(config, "fast")  # alias without provider_id -> first provider
    assert derived.gateway_base_url == GW and derived.gateway_credential_configured
    read = public._read(config)
    assert read.providers[0].id == "main" and read.providers[0].models[0].type == "embedding"
    assert "k1" not in read.model_dump_json()


@pytest.mark.asyncio
async def test_no_row_defaults_to_unconfigured_without_env_bootstrap() -> None:
    config = await _config(None)  # type: ignore[arg-type]
    assert config.providers == {} and config.gateway_base_url is None and config.aliases == {}


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
    assert not config.providers["y"].local and config.providers["y"].destination_id.startswith("remote:")  # type: ignore[union-attr]
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
        providers=[_provider("cloud", api_key="sk-secret", credential_action="replaced"),
                   _provider("ol", "ollama", OLLAMA, models=[{"id": "llama3", "type": "llm"}])],  # type: ignore[arg-type]
        aliases={"local-private": ModelMapping(model="llama3", provider_id="ol")}))
    assert "sk-secret" not in str(row.providers)
    assert Fernet(KEY.encode()).decrypt(row.providers["cloud"]["key_ciphertext"].encode()).decode() == "sk-secret"
    assert row.providers["ol"]["local"] is True and row.providers["cloud"]["local"] is False


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
async def test_absent_providers_keeps_them_untouched() -> None:
    other = {"kind": "ollama", "label": "O", "base_url": OLLAMA, "key_ciphertext": None, "local": True,
             "models": [{"id": "llama3", "type": "llm"}]}
    row = _row(providers={"o": other})
    await _save(row, _update())
    assert row.providers == {"o": other}


def _typed(pid: str = "a", **models: str) -> ProviderUpdate:
    return _provider(pid, models=[{"id": mid, "type": mtype} for mid, mtype in models.items()])  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(("alias", "mtype", "ok"), [
    ("embedding", "embedding", True), ("embedding", "llm", False),
    ("reranker", "reranker", True), ("reranker", "embedding", False),
    ("vision", "vision", True), ("vision", "llm", False),
    ("fast", "llm", True), ("reasoning-large", "embedding", False), ("reasoning-small", "tts", False),
])
async def test_alias_requires_matching_model_type(alias: str, mtype: str, ok: bool) -> None:
    dims = 8 if alias == "embedding" else None
    update = _update(providers=[_typed("a", m1=mtype)], aliases={alias: ModelMapping(model="m1", provider_id="a", dimensions=dims)})
    if ok:
        row = await _save(_row(), update)
        assert row.providers["a"]["models"] == [{"id": "m1", "type": mtype}]
    else:
        with pytest.raises(HTTPException) as exc:
            await _save(_row(), update)
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_unlisted_model_is_rejected_when_listed_and_duplicate_model_ids_rejected() -> None:
    with pytest.raises(HTTPException) as exc:  # provider has a model list -> unlisted id is 422
        await _save(_row(), _update(providers=[_typed("a", m1="llm")], aliases={"embedding": ModelMapping(model="other", provider_id="a")}))
    assert exc.value.status_code == 422
    # empty model list = not yet discovered -> a chosen model is rejected until discovery runs
    with pytest.raises(HTTPException) as exc:
        await _save(_row(), _update(providers=[_provider("a")], aliases={"embedding": ModelMapping(model="other", provider_id="a", dimensions=8)}))
    assert exc.value.status_code == 422 and "Discover" in exc.value.detail
    with pytest.raises(HTTPException):
        await _save(_row(), _update(providers=[_provider("a", models=[{"id": "m", "type": "llm"}, {"id": "m", "type": "tts"}])]))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_embedding_dimensions_required_rejected_elsewhere_and_round_trip() -> None:
    providers = [_typed("a", e="embedding", c="llm")]
    for aliases in ({"embedding": ModelMapping(model="e", provider_id="a")},  # missing dimensions
                    {"fast": ModelMapping(model="c", provider_id="a", dimensions=8)}):  # dimensions on non-embedding
        with pytest.raises(HTTPException) as exc:
            await _save(_row(), _update(providers=providers, aliases=aliases))
        assert exc.value.status_code == 422
    row = await _save(_row(), _update(providers=providers, aliases={"embedding": ModelMapping(model="e", provider_id="a", dimensions=768)}))
    assert row.aliases["embedding"]["dimensions"] == 768


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
async def test_toggle_keeps_grant_for_unchanged_remote_provider_and_local_never_gets_one() -> None:
    row = _row(providers={"default": {"kind": "custom", "label": "D", "base_url": GW, "key_ciphertext": None},
                          "ol": {"kind": "ollama", "label": "O", "base_url": OLLAMA, "key_ciphertext": None}})
    await _save(row, _update(privacy=PrivacySettings(allow_remote_reasoning=True,
                                                     reasoning_destinations=[_dest(GW), _dest(OLLAMA, True)])))
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
    assert (key, local) == ("default-key", False) and destination.startswith("remote:")
    assert public.resolve_draft_provider(config, "https://evil.example", "", None, None)[0] == ""
    assert public.resolve_draft_provider(config, "https://d.example", "typed", None, None)[0] == "typed"
    key, local, destination = public.resolve_draft_provider(config, "http://x:11434", "", None, "ollama")
    assert (key, local) == ("", True) and destination.startswith("local:")


# ---- Redis degradation ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_aliases_degrades_to_defaults_when_redis_is_down() -> None:
    settings = _settings()
    redis = MagicMock()
    redis.hgetall = AsyncMock(side_effect=RedisError("down"))
    aliases = await models.legacy_aliases(redis, settings, scope=SCOPE)
    assert aliases == {}


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
