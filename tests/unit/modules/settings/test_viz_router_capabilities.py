"""Router probes publish exact configured-model evidence that runtime gateway calls can reuse."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from redis.asyncio import Redis

from core.model_gateway.client import ModelGateway
from core.model_gateway.schemas import (
    AIExecutionConfig,
    ModelDefaults,
    ModelMapping,
    PrivacySettings,
    ProviderConfig,
    RequestPolicy,
)
from core.workspaces.schemas import WorkspaceContext
from modules.settings import public, routes

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)


def _base() -> AIExecutionConfig:
    """Build an unrelated chat default and a separate decision provider without production state."""
    return AIExecutionConfig(
        workspace_id=SCOPE.workspace_id, actor_user_id=1, membership_revision=1, access_configuration_revision=1,
        configuration_revision=1, gateway_identity="a" * 64, endpoint_destination_id="remote:chat",
        gateway_base_url="https://chat.example/v1", gateway_api_key="chat-key",
        defaults=ModelDefaults(chat=ModelMapping(model="chat-model")), privacy=PrivacySettings(),
        request_timeout_seconds=20, web_search_provider="none", web_search_endpoint=None, web_search_api_key="",
        providers={"decision": ProviderConfig(kind="custom", base_url="https://decision.example/v1",
                                             api_key="decision-key", destination_id="remote:decision")},
    )


def _router(kind: str = "llm", mode: str = "json_schema", api: str = "chat") -> public.VizRouterResolved:
    """Resolve a synthetic router whose model never occupies a role-default slot."""
    return public.VizRouterResolved(
        kind=kind, provider_id="decision" if kind == "llm" else None, model="decision-model", mode=mode, api=api,
        base_url="https://router.example/v1", api_key="router-key", destination_id="remote:router", allow_remote=True,
    )


@pytest.mark.parametrize("kind,mode,api", [
    (kind, mode, "chat") for kind in ("llm", "openrouter", "cloudflare") for mode in ("json_schema", "forced_tool")
] + [("openrouter", "json_schema", "decisions")])
async def test_successful_probe_publishes_evidence_consumed_by_runtime(kind: str, mode: str, api: str) -> None:
    """Exercise real evidence creation/storage and the runtime cache gate for every gateway adapter."""
    base, resolved = _base(), _router(kind, mode, api)
    config, mapping = public.viz_router_gateway_config(base, resolved)
    cached: dict[str, str] = {}
    redis = AsyncMock(spec=Redis)
    redis.set = AsyncMock(side_effect=lambda key, value, **kwargs: cached.update({key: value}))
    redis.get = AsyncMock(side_effect=lambda key: cached.get(key))
    request, session = MagicMock(), MagicMock()
    request.app.state.settings.multi_workspace_enabled = False
    request.app.state.redis = redis
    session.rollback = AsyncMock()
    with patch.object(public, "resolve_viz_router", AsyncMock(return_value=resolved)), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=base)), \
         patch.object(public, "check_ai_execution_config", AsyncMock()), \
         patch.object(routes, "authenticated_session_ref", MagicMock()), \
         patch.object(routes, "ModelGateway", MagicMock()), \
         patch.object(routes.viz, "classify_llm", AsyncMock(return_value="table")), \
         patch.object(routes.viz, "classify_decisions", AsyncMock(return_value="table")):
        result = await routes.test_viz_router(session, request, SCOPE)
    assert result.ok and result.category == "table"
    gateway = ModelGateway(redis, config.gateway_base_url, config.gateway_api_key, config.endpoint_destination_id,
                           scope=SCOPE, configuration_revision=1, gateway_identity=config.gateway_identity,
                           before_send=AsyncMock())
    policy = RequestPolicy(workspace_id=SCOPE.workspace_id, actor_user_id=1, membership_revision=1,
                           configuration_revision=1, gateway_identity=config.gateway_identity)
    capability = "tools" if mode == "forced_tool" else "structured"
    assert await gateway._capability_supported(public.VIZ_ROUTER_ROLE, mapping, policy, capability)
    assert len(cached) == 1 and "router-key" not in str(cached) and "decision-key" not in str(cached)


@pytest.mark.parametrize("change", [
    {"revision": 2}, {"base_url": "https://other.example/v1"}, {"api_key": "new-key"},
    {"destination_id": "remote:other"}, {"api": "decisions"},
])
def test_router_capability_namespace_changes_with_router_configuration(change: dict[str, object]) -> None:
    """Evidence cannot follow an identical model name to another endpoint, credential, API or revision."""
    base, resolved = _base(), _router("openrouter")
    original, _ = public.viz_router_gateway_config(base, resolved)
    changed, _ = public.viz_router_gateway_config(base, resolved.model_copy(update=change))
    assert original.gateway_identity != changed.gateway_identity
    assert original.gateway_identity != base.gateway_identity


@pytest.mark.parametrize("stage", ["send", "publish"])
async def test_router_revision_drift_rejects_probe_without_publishing(stage: str) -> None:
    """An in-flight probe must reject a newly saved router before egress or capability publication."""
    resolved, base = _router(), _base()
    request, session = MagicMock(), MagicMock()
    request.app.state.settings.multi_workspace_enabled = False
    request.app.state.redis = AsyncMock(spec=Redis)
    request.app.state.redis.set = AsyncMock()
    session.rollback = AsyncMock()

    async def classify(*args: object, **kwargs: object) -> str:
        """Simulate a gateway send fence only in the pre-egress race case."""
        if stage == "send":
            await kwargs["before_send"]()
        return "table"

    with patch.object(public, "resolve_viz_router", AsyncMock(side_effect=[resolved, resolved.model_copy(update={"revision": 2})])), \
         patch.object(public, "get_ai_execution_config", AsyncMock(return_value=base)), \
         patch.object(public, "check_ai_execution_config", AsyncMock()), \
         patch.object(routes, "authenticated_session_ref", MagicMock()), \
         patch.object(routes, "ModelGateway", MagicMock()), \
         patch.object(routes.viz, "classify_llm", classify), \
         pytest.raises(HTTPException) as exc:
        await routes.test_viz_router(session, request, SCOPE)
    assert exc.value.status_code == 409
    request.app.state.redis.set.assert_not_awaited()
    assert session.rollback.await_count >= 2


@pytest.mark.parametrize("role,model,version", [
    (public.VIZ_ROUTER_ROLE, "other-model", None), (public.VIZ_ROUTER_ROLE, "decision-model", "other-version"),
    ("chat", "decision-model", None),
])
async def test_explicit_router_mapping_preserves_model_and_default_role_validation(role: str, model: str, version: str | None) -> None:
    """An explicit non-default mapping cannot authorize mismatched evidence or override a default role."""
    config, mapping = public.viz_router_gateway_config(_base(), _router())
    evidence = public.new_capability_result(role, mapping, "structured", "supported", config=config,
                                            scope=SCOPE, multi_workspace_enabled=False)
    evidence = evidence.model_copy(update={"model": model, "version": version})
    redis = AsyncMock(spec=Redis)
    redis.set = AsyncMock()
    with pytest.raises(ValueError, match="configured model"):
        await public.save_capability(redis, evidence, config=config, configured_mapping=mapping,
                                     scope=SCOPE, multi_workspace_enabled=False)
    redis.set.assert_not_awaited()
