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
                "models": [{"id": "gpt-x", "type": "llm"}, {"id": "emb", "type": "embedding"}]}
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
