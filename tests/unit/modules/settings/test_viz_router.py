"""Visualization router settings: save validation, destination-bound consent, key secrecy and the Test routing route."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr

from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.settings import public, routes
from modules.settings.schemas import VizRouterUpdate

WS = uuid4()
SCOPE = WorkspaceContext(user_id=1, workspace_id=WS, role="owner", membership_revision=1)
KEY = Fernet.generate_key().decode()
GW = "https://gw.example/v1"


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.ai_allowed_endpoint_hosts = {"gw.example", "api.typesafe.ai"}
    settings.ai_allowed_endpoint_cidrs = []
    settings.web_search_allowed_cidrs = []
    settings.ai_allow_public_providers = True
    settings.ai_credential_encryption_key = SecretStr(KEY)
    settings.ai_credential_encryption_key_secondary = SecretStr("")
    settings.multi_workspace_enabled = False
    return settings


def _row(viz: dict | None = None) -> SimpleNamespace:
    provider = {"kind": "openai", "label": "gw", "base_url": GW, "key_ciphertext": None,
                "models": [{"id": "gpt-x", "type": "llm"}, {"id": "emb", "type": "embedding"},
                           {"id": "dec", "type": "decision"}]}
    return SimpleNamespace(providers={"default": provider}, viz_router=viz or {})


async def _save(row: SimpleNamespace, **fields: object) -> object:
    update = VizRouterUpdate(expected_revision=int((row.viz_router or {}).get("revision", 1)), **fields)  # type: ignore[arg-type]
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)), \
         patch.object(public, "read_viz_router", AsyncMock()):
        session = MagicMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()
        await public.save_viz_router(session, update, _settings(), scope=SCOPE)
    return row.viz_router


async def _resolve(row: SimpleNamespace) -> public.VizRouterResolved:
    with patch.object(public, "_row", AsyncMock(return_value=row)):
        return await public.resolve_viz_router(MagicMock(), _settings(), scope=SCOPE)


@pytest.mark.asyncio
async def test_default_is_internal_and_consent_flag_off() -> None:
    resolved = await _resolve(_row())
    assert (resolved.kind, resolved.allow_remote) == ("internal", False)


@pytest.mark.asyncio
async def test_llm_save_validates_model_and_binds_consent_to_destination() -> None:
    row = _row()
    stored = await _save(row, kind="llm", provider_id="default", model="gpt-x", mode="forced_tool",
                         allow_remote_visualization_routing=True)
    assert stored["mode"] == "forced_tool" and stored["revision"] == 2
    resolved = await _resolve(row)
    assert resolved.allow_remote and resolved.destination_id and resolved.destination_id.startswith("remote:")
    row.providers["default"]["base_url"] = "https://gw.example/other"  # endpoint edited later: consent no longer matches
    assert (await _resolve(row)).allow_remote is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["missing", "emb"])
async def test_llm_save_rejects_unknown_or_non_llm_model(model: str) -> None:
    with pytest.raises(HTTPException) as err:
        await _save(_row(), kind="llm", provider_id="default", model=model)
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_decision_model_is_accepted_for_the_router() -> None:
    stored = await _save(_row(), kind="llm", provider_id="default", model="dec")
    assert stored["model"] == "dec"


@pytest.mark.asyncio
async def test_stale_revision_conflicts() -> None:
    row = _row({"kind": "internal", "revision": 3})
    update = VizRouterUpdate(expected_revision=1)
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)):
        session = MagicMock()
        session.execute = AsyncMock()
        with pytest.raises(HTTPException) as err:
            await public.save_viz_router(session, update, _settings(), scope=SCOPE)
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_typesafe_key_is_encrypted_and_never_in_repr() -> None:
    row = _row()
    stored = await _save(row, kind="typesafe", typesafe_key_action="replaced", typesafe_key="ts-secret-key",
                         allow_remote_visualization_routing=True)
    assert "ts-secret-key" not in str(stored)
    resolved = await _resolve(row)
    assert resolved.typesafe_key == "ts-secret-key" and resolved.allow_remote
    assert "ts-secret-key" not in repr(resolved)
    kept = await _save(row, kind="typesafe", allow_remote_visualization_routing=False)  # key unchanged, consent dropped
    assert kept["typesafe_key_ciphertext"] == stored["typesafe_key_ciphertext"]
    assert (await _resolve(row)).allow_remote is False


# ---- Test routing route -------------------------------------------------------------------------------------

def _request() -> MagicMock:
    request = MagicMock()
    request.app.state.settings = _settings()
    request.app.state.redis = MagicMock()
    return request


async def _run_test(resolved: public.VizRouterResolved, transport: httpx.MockTransport | None = None) -> object:
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(routes, "approved_web_search_transport", return_value=transport):
        session = MagicMock()
        session.rollback = AsyncMock()
        return await routes.test_viz_router(session, _request(), SCOPE)


@pytest.mark.asyncio
async def test_route_refuses_internal_and_missing_consent() -> None:
    assert (await _run_test(public.VizRouterResolved())).error == "internal_router"  # type: ignore[attr-defined]
    off = public.VizRouterResolved(kind="typesafe", typesafe_key="k", allow_remote=False)
    assert (await _run_test(off)).error == "consent_required"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_route_typesafe_success_and_401() -> None:
    resolved = public.VizRouterResolved(kind="typesafe", typesafe_key="k", allow_remote=True)
    good = httpx.MockTransport(lambda r: httpx.Response(200, json={"answers": {"visualization": {
        "type": "choice", "choice": "line_chart", "probabilities": {"line_chart": 0.9}}}}))
    result = await _run_test(resolved, good)
    assert (result.ok, result.category) == (True, "line_chart")  # type: ignore[attr-defined]
    bad = await _run_test(resolved, httpx.MockTransport(lambda r: httpx.Response(401)))
    assert (bad.ok, bad.error) == (False, "auth_error")  # type: ignore[attr-defined]
    missing = await _run_test(public.VizRouterResolved(kind="typesafe", allow_remote=True), good)
    assert missing.error == "credential_required"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_route_llm_falls_back_to_other_mode_and_records_capability() -> None:
    resolved = public.VizRouterResolved(kind="llm", provider_id="default", model="gpt-x", allow_remote=True)
    config = SimpleNamespace(
        workspace_id=WS, actor_user_id=1, membership_revision=1, gateway_identity="a" * 64, configuration_revision=1,
        endpoint_destination_id="remote:abc", gateway_base_url=GW, gateway_api_key="k", endpoint_allowed_cidrs=(),
        allow_public_providers=True)
    mapping = SimpleNamespace(model="gpt-x")
    calls: list[str] = []

    async def fake_classify(gateway: object, mapping: object, policy: object, window: object, *, mode: str, **kw: object) -> str:
        calls.append(mode)
        if mode == "json_schema":
            raise routes.viz.VisualizationRouterError("mode_unsupported", retryable=False)
        return "table"

    saved = AsyncMock()
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_router_gateway_config", return_value=(config, mapping)), \
         patch.object(public, "new_capability_result", return_value="result") as new_result, \
         patch.object(public, "check_ai_execution_config", AsyncMock()), \
         patch.object(public, "save_capability", saved), \
         patch.object(routes, "ModelGateway", MagicMock()), \
         patch.object(routes, "authenticated_session_ref", MagicMock()), \
         patch.object(routes.viz, "classify_llm", fake_classify):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.test_viz_router(session, _request(), SCOPE)
    assert calls == ["json_schema", "forced_tool"]
    assert (result.ok, result.mode, result.category) == (True, "forced_tool", "table")
    assert new_result.call_args.args[2] == "tools" and saved.await_count == 1


@pytest.mark.asyncio
async def test_route_llm_hard_failure_does_not_try_other_mode() -> None:
    resolved = public.VizRouterResolved(kind="llm", provider_id="default", model="gpt-x", allow_remote=True)
    config = SimpleNamespace(
        workspace_id=WS, actor_user_id=1, membership_revision=1, gateway_identity="a" * 64, configuration_revision=1,
        endpoint_destination_id="remote:abc", gateway_base_url=GW, gateway_api_key="k", endpoint_allowed_cidrs=(),
        allow_public_providers=True)
    seen: list[str] = []

    async def fail(gateway: object, mapping: object, policy: object, window: object, *, mode: str, **kw: object) -> str:
        seen.append(mode)
        raise routes.viz.VisualizationRouterError("auth_error", retryable=False)

    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_router_gateway_config", return_value=(config, SimpleNamespace(model="gpt-x"))), \
         patch.object(routes, "ModelGateway", MagicMock()), \
         patch.object(routes, "authenticated_session_ref", MagicMock()), \
         patch.object(routes.viz, "classify_llm", fail):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.test_viz_router(session, _request(), SCOPE)
    assert (result.ok, result.error, seen) == (False, "auth_error", ["json_schema"])


# ---- openrouter / cloudflare (self-contained kinds) ---------------------------------------------------------

ACCOUNT = "0123456789abcdef0123456789abcdef"


def _keyed_settings() -> MagicMock:
    settings = _settings()
    settings.ai_allowed_endpoint_hosts = set()
    settings.ai_allow_public_providers = True
    return settings


async def _save_keyed(row: SimpleNamespace, settings: MagicMock | None = None, **fields: object) -> object:
    update = VizRouterUpdate(expected_revision=int((row.viz_router or {}).get("revision", 1)), **fields)  # type: ignore[arg-type]
    fence = AccessFence(workspace_id=WS, user_id=1, membership_revision=1, configuration_revision=1)
    with patch.object(public, "require_settings_scope", AsyncMock(return_value=fence)), \
         patch.object(public, "_row", AsyncMock(return_value=row)), \
         patch.object(public, "read_viz_router", AsyncMock()):
        session = MagicMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()
        await public.save_viz_router(session, update, settings or _keyed_settings(), scope=SCOPE)
    return row.viz_router


async def _resolve_keyed(row: SimpleNamespace, settings: MagicMock | None = None) -> public.VizRouterResolved:
    with patch.object(public, "_row", AsyncMock(return_value=row)):
        return await public.resolve_viz_router(MagicMock(), settings or _keyed_settings(), scope=SCOPE)


@pytest.mark.asyncio
async def test_openrouter_save_encrypts_key_binds_consent_and_resolves_endpoint() -> None:
    row = _row()
    stored = await _save_keyed(row, kind="openrouter", model="openrouter/auto", mode="forced_tool",
                               api_key_action="replaced", api_key="or-secret", allow_remote_visualization_routing=True)
    assert "or-secret" not in str(stored) and stored["openrouter_key_ciphertext"]
    resolved = await _resolve_keyed(row)
    assert (resolved.base_url, resolved.model, resolved.mode, resolved.api_key) == (
        public.VIZ_OPENROUTER_URL, "openrouter/auto", "forced_tool", "or-secret")
    assert resolved.allow_remote and resolved.destination_id and resolved.destination_id.startswith("remote:")
    assert "or-secret" not in repr(resolved)


@pytest.mark.asyncio
async def test_keyed_kind_requires_model_and_key() -> None:
    for fields in ({"kind": "openrouter", "model": "", "api_key_action": "replaced", "api_key": "k"},
                   {"kind": "openrouter", "model": "m"}):
        with pytest.raises(HTTPException) as err:
            await _save_keyed(_row(), **fields)
        assert err.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, "", "abc", "g" * 32, ACCOUNT + "0"])
async def test_cloudflare_rejects_bad_account_id(account: str | None) -> None:
    with pytest.raises(HTTPException) as err:
        await _save_keyed(_row(), kind="cloudflare", model="@cf/meta/llama-3.1-8b-instruct", account_id=account,
                          api_key_action="replaced", api_key="k")
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_cloudflare_account_endpoint_and_consent_dropped_when_destination_changes() -> None:
    row = _row()
    await _save_keyed(row, kind="cloudflare", model="@cf/m", account_id=ACCOUNT.upper(), api_key_action="replaced",
                      api_key="cf-secret", allow_remote_visualization_routing=True)
    resolved = await _resolve_keyed(row)
    assert resolved.base_url == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1"
    assert resolved.allow_remote and resolved.api_key == "cf-secret"
    row.viz_router["account_id"] = "f" * 32  # destination changes: stored consent no longer matches
    assert (await _resolve_keyed(row)).allow_remote is False
    # re-saving with another gateway URL and the consent box off also leaves consent off
    await _save_keyed(row, kind="cloudflare", model="@cf/m", gateway_url="https://gateway.ai.cloudflare.com/v1/x/y/openai")
    assert (await _resolve_keyed(row)).allow_remote is False


@pytest.mark.asyncio
async def test_cloudflare_gateway_override_must_be_https_and_policy_allowed() -> None:
    kw = {"kind": "cloudflare", "model": "m", "api_key_action": "replaced", "api_key": "k"}
    with pytest.raises(HTTPException):
        await _save_keyed(_row(), gateway_url="http://gateway.ai.cloudflare.com/v1/x", **kw)  # type: ignore[arg-type]
    denied = _keyed_settings()
    denied.ai_allow_public_providers = False
    with pytest.raises(HTTPException) as err:
        await _save_keyed(_row(), denied, account_id=ACCOUNT, **kw)  # type: ignore[arg-type]
    assert err.value.status_code == 422
    with pytest.raises(HTTPException):
        await _save_keyed(_row(), denied, kind="openrouter", model="m", api_key_action="replaced", api_key="k")


@pytest.mark.asyncio
async def test_keys_are_kept_per_kind_and_never_reused_across_hosts() -> None:
    row = _row()
    await _save_keyed(row, kind="openrouter", model="m", api_key_action="replaced", api_key="or-key")
    stored = await _save_keyed(row, kind="cloudflare", model="m", account_id=ACCOUNT, api_key_action="replaced", api_key="cf-key")
    assert stored["openrouter_key_ciphertext"] and stored["cloudflare_key_ciphertext"]
    assert (await _resolve_keyed(row)).api_key == "cf-key"
    with pytest.raises(HTTPException):  # cloudflare key removed: openrouter's is not borrowed
        await _save_keyed(row, kind="cloudflare", model="m", account_id=ACCOUNT, api_key_action="removed")


def test_gateway_config_points_at_own_endpoint_and_key() -> None:
    from core.model_gateway.schemas import AIExecutionConfig, ModelDefaults, PrivacySettings

    base = AIExecutionConfig(
        workspace_id=WS, actor_user_id=1, membership_revision=1, access_configuration_revision=1, configuration_revision=1,
        gateway_identity="a" * 64, endpoint_destination_id="remote:old", gateway_base_url=GW, gateway_api_key="provider-key",
        defaults=ModelDefaults(), privacy=PrivacySettings(), request_timeout_seconds=20, web_search_provider="none",
        web_search_endpoint=None, web_search_api_key="")
    resolved = public.VizRouterResolved(kind="openrouter", model="openrouter/auto", base_url=public.VIZ_OPENROUTER_URL,
                                        api_key="or-key", allow_remote=True, destination_id="remote:xyz")
    config, mapping = public.viz_router_gateway_config(base, resolved)
    assert (config.gateway_base_url, config.gateway_api_key, config.endpoint_destination_id) == (
        public.VIZ_OPENROUTER_URL, "or-key", "remote:xyz")
    assert (mapping.model, mapping.destination) == ("openrouter/auto", "remote")


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved,error", [
    (public.VizRouterResolved(kind="openrouter", model="m", base_url=public.VIZ_OPENROUTER_URL, allow_remote=True), "credential_required"),
    (public.VizRouterResolved(kind="cloudflare", model="m", api_key="k", allow_remote=True), "network_denied"),
])
async def test_route_keyed_kind_missing_credential_or_endpoint(resolved: public.VizRouterResolved, error: str) -> None:
    config = SimpleNamespace(endpoint_destination_id=None)
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_router_gateway_config", return_value=(config, SimpleNamespace(model="m"))):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.test_viz_router(session, _request(), SCOPE)
    assert (result.ok, result.error) == (False, error)


# ---- openrouter Decisions API selector and discovery route --------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_api_defaults_to_chat_and_decisions_is_stored_only_for_openrouter() -> None:
    row = _row()
    stored = await _save_keyed(row, kind="openrouter", model="m", api_key_action="replaced", api_key="k",
                               allow_remote_visualization_routing=True)
    assert "api" not in stored and (await _resolve_keyed(row)).api == "chat"  # legacy rows keep the chat path
    stored = await _save_keyed(row, kind="openrouter", model="cloudflare/clef-flash", api="decisions")
    assert stored["api"] == "decisions" and (await _resolve_keyed(row)).api == "decisions"
    cf = await _save_keyed(_row(), kind="cloudflare", model="m", account_id=ACCOUNT, api="decisions",
                           api_key_action="replaced", api_key="cf")
    assert "api" not in cf


@pytest.mark.asyncio
async def test_route_decisions_api_uses_decisions_adapter_and_records_structured() -> None:
    resolved = public.VizRouterResolved(kind="openrouter", model="cloudflare/clef-flash", api="decisions",
                                        base_url=public.VIZ_OPENROUTER_URL, api_key="k", allow_remote=True)
    config = SimpleNamespace(
        workspace_id=WS, actor_user_id=1, membership_revision=1, gateway_identity="a" * 64, configuration_revision=1,
        endpoint_destination_id="remote:abc", gateway_base_url=public.VIZ_OPENROUTER_URL, gateway_api_key="k",
        endpoint_allowed_cidrs=(), allow_public_providers=True)
    chat = AsyncMock()
    decisions = AsyncMock(return_value="map")
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_router_gateway_config", return_value=(config, SimpleNamespace(model="m"))), \
         patch.object(public, "new_capability_result", return_value="r") as new_result, \
         patch.object(public, "check_ai_execution_config", AsyncMock()), \
         patch.object(public, "save_capability", AsyncMock()), \
         patch.object(routes, "ModelGateway", MagicMock()), \
         patch.object(routes, "authenticated_session_ref", MagicMock()), \
         patch.object(routes.viz, "classify_llm", chat), \
         patch.object(routes.viz, "classify_decisions", decisions):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.test_viz_router(session, _request(), SCOPE)
    assert (result.ok, result.category, result.mode) == (True, "map", None)
    assert decisions.await_count == 1 and chat.await_count == 0 and new_result.call_args.args[2] == "structured"


async def _discover(body: object, resolved: public.VizRouterResolved, gateway: object) -> object:
    config = SimpleNamespace(
        gateway_base_url=public.VIZ_OPENROUTER_URL, gateway_api_key="", endpoint_destination_id="remote:abc",
        configuration_revision=1, gateway_identity="a" * 64, endpoint_allowed_cidrs=(), allow_public_providers=True)
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_decision_discovery_config", return_value=config) as discovery_config, \
         patch.object(routes, "ModelGateway", MagicMock(return_value=gateway)), \
         patch.object(routes, "authenticated_session_ref", MagicMock()):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.discover_viz_decision_models(body, session, _request(), SCOPE)  # type: ignore[arg-type]
    return result, discovery_config


@pytest.mark.asyncio
async def test_discovery_needs_a_key_and_never_borrows_another_kinds_key() -> None:
    from modules.settings.schemas import VizDecisionModelsRequest

    gateway = SimpleNamespace(discover_decision_models=AsyncMock())
    cloudflare = public.VizRouterResolved(kind="cloudflare", api_key="cf-key")
    result, _ = await _discover(VizDecisionModelsRequest(), cloudflare, gateway)
    assert (result.ok, result.error) == (False, "credential_required") and gateway.discover_decision_models.await_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_discovery_returns_bounded_validated_models_with_transient_or_saved_key() -> None:
    from modules.settings.schemas import VizDecisionModelsRequest

    payload = {"data": [{"id": f"v/m{i}", "architecture": {"input_modalities": ["text", "image"]}} for i in range(500)]
               + [{"id": "bad id"}]}
    gateway = SimpleNamespace(discover_decision_models=AsyncMock(return_value=payload))
    result, cfg = await _discover(VizDecisionModelsRequest(api_key="transient"), public.VizRouterResolved(), gateway)
    assert result.ok and len(result.models) == 200 and result.models[0].input_modalities == ["image", "text"]  # type: ignore[attr-defined]
    assert cfg.call_args.args[1] == "transient"
    saved = public.VizRouterResolved(kind="openrouter", api_key="saved-key")
    _, cfg = await _discover(VizDecisionModelsRequest(), saved, gateway)
    assert cfg.call_args.args[1] == "saved-key"


@pytest.mark.asyncio
async def test_discovery_failure_returns_short_code() -> None:
    from core.model_gateway.client import ModelGatewayError
    from modules.settings.schemas import VizDecisionModelsRequest

    gateway = SimpleNamespace(discover_decision_models=AsyncMock(side_effect=ModelGatewayError("upstream returned HTTP 401")))
    result, _ = await _discover(VizDecisionModelsRequest(api_key="k"), public.VizRouterResolved(), gateway)
    assert (result.ok, result.error, result.models) == (False, "auth_error", [])  # type: ignore[attr-defined]
