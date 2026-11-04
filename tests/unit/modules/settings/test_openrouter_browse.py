from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from core.model_gateway.client import ModelGatewayError
from core.model_gateway.schemas import ProviderConfig
from modules.settings import public, routes
from modules.settings.openrouter_models import (
    OpenRouterModelFilters,
    parse_openrouter_models,
    to_query_params,
)
from modules.settings.schemas import OpenRouterBrowseRequest

SCOPE = MagicMock()
OR_URL = "https://openrouter.ai/api/v1"


def test_filters_reject_unknown_keys_and_inverted_ranges():
    with pytest.raises(ValidationError):
        OpenRouterModelFilters.model_validate({"foo": 1})
    with pytest.raises(ValidationError):
        OpenRouterModelFilters(min_price=5, max_price=1)
    with pytest.raises(ValidationError):
        OpenRouterModelFilters(sort="cheapest")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        OpenRouterModelFilters(model_authors=["Bad Author!"])


def test_query_params_cover_every_filter():
    f = OpenRouterModelFilters(
        output_modalities="image", input_modalities=["text", "image"], category="programming",
        supported_parameters=["tools"], sort="pricing-low-to-high", q="flux", endpoints=["/api/v1/images"], context=8000,
        min_price=0, max_price=2.5, min_output_price=0, max_output_price=10, min_age_days=0, max_age_days=30, arch="Claude",
        model_authors=["openai", "google"], providers=["Together"], distillable=True, zdr=True, region="eu",
        min_intelligence_index=10, max_intelligence_index=90, min_coding_index=1, max_coding_index=2,
        min_agentic_index=1, max_agentic_index=2, min_tool_success_rate=0.5, max_tool_success_rate=1)
    p = to_query_params(f, offset=50, limit=50)
    assert p["output_modalities"] == "image" and p["input_modalities"] == "text,image" and p["model_authors"] == "openai,google"
    assert p["zdr"] == "true" and p["distillable"] == "true" and p["region"] == "eu" and p["offset"] == "50" and p["limit"] == "50"
    assert set(p) == {*OpenRouterModelFilters.model_fields} | {"offset", "limit"}
    assert "zdr" not in to_query_params(OpenRouterModelFilters(), 0, 50)
    assert to_query_params(OpenRouterModelFilters(distillable=False), 0, 50)["distillable"] == "false"


def test_parse_openrouter_models_bounds_and_skips_malformed():
    payload = {"total_count": 3, "links": {"next": "https://openrouter.ai/api/v1/models?offset=2"}, "data": [
        {"id": "black-forest-labs/flux-2", "name": "FLUX.2", "created": 1760000000, "context_length": 4096,
         "architecture": {"input_modalities": ["text", "bogus"], "output_modalities": ["image"]},
         "pricing": {"prompt": "0.000001", "completion": "nan?"}, "supported_parameters": ["seed", "BAD-PARAM"]},
        {"id": "bad id with spaces"}, {"id": "black-forest-labs/flux-2"}, "junk"]}
    models, total, has_next = parse_openrouter_models(payload, limit=50)
    assert [m["id"] for m in models] == ["black-forest-labs/flux-2"] and total == 3 and has_next
    m = models[0]
    assert m["input_modalities"] == ["text"] and m["output_modalities"] == ["image"]
    assert m["pricing_prompt"] == "0.000001" and m["pricing_completion"] is None and m["supported_parameters"] == ["seed"]
    many = {"total_count": 10**9, "data": [{"id": f"v/m{i}"} for i in range(500)]}
    models, total, has_next = parse_openrouter_models(many, limit=7)
    assert len(models) == 7 and total == 1_000_000 and not has_next
    assert parse_openrouter_models("junk", 5) == ([], 0, False)


def _config(providers: dict[str, ProviderConfig]) -> SimpleNamespace:
    return SimpleNamespace(
        providers=providers, gateway_base_url=OR_URL, gateway_api_key="", gateway_kind="openrouter",
        endpoint_destination_id="remote:abc", configuration_revision=1, gateway_identity="a" * 64,
        model_identity="a" * 64, endpoint_allowed_cidrs=(), allow_public_providers=True)


async def _browse(body: OpenRouterBrowseRequest, config: SimpleNamespace, gateway: object):
    with patch.object(public, "get_ai_execution_config", AsyncMock(return_value=config)), \
         patch.object(public, "viz_decision_discovery_config", return_value=config) as discovery, \
         patch.object(routes, "ModelGateway", MagicMock(return_value=gateway)) as ctor, \
         patch.object(routes, "authenticated_session_ref", MagicMock()):
        session = MagicMock()
        session.rollback = AsyncMock()
        result = await routes.browse_openrouter_models(body, session, MagicMock(), SCOPE)  # type: ignore[arg-type]
    return result, discovery, ctor


@pytest.mark.asyncio
async def test_browse_route_uses_saved_key_only_for_openrouter_provider():
    openai = ProviderConfig(kind="openai", base_url="https://api.openai.com/v1", api_key="sk-openai", destination_id="d")
    gateway = SimpleNamespace(list_openrouter_models=AsyncMock())
    result, _, ctor = await _browse(OpenRouterBrowseRequest(provider_id="p1"), _config({"p1": openai}), gateway)
    assert (result.ok, result.error) == (False, "credential_required")
    assert ctor.call_count == 0 and gateway.list_openrouter_models.await_count == 0
    # An openrouter-kind row pointed at a custom proxy must not leak its key to openrouter.ai.
    proxy = ProviderConfig(kind="openrouter", base_url="https://proxy.example/v1", api_key="sk-or", destination_id="d")
    result, _, _ = await _browse(OpenRouterBrowseRequest(provider_id="p1"), _config({"p1": proxy}), gateway)
    assert result.error == "credential_required"


@pytest.mark.asyncio
async def test_browse_route_sends_filters_to_fixed_endpoint():
    saved = ProviderConfig(kind="openrouter", base_url=OR_URL, api_key="sk-or", destination_id="d")
    payload = {"total_count": 9, "links": {"next": "x"}, "data": [{"id": "v/a"}, {"id": "v/b"}]}
    gateway = SimpleNamespace(list_openrouter_models=AsyncMock(return_value=payload))
    body = OpenRouterBrowseRequest(provider_id="p1", offset=10, limit=2, filters=OpenRouterModelFilters(q="flux"))
    result, discovery, ctor = await _browse(body, _config({"p1": saved}), gateway)
    assert result.ok and result.next_offset == 12 and result.total_count == 9
    assert discovery.call_args.args[1] == "sk-or" and ctor.call_args.kwargs["kind"] == "openrouter"
    gateway.list_openrouter_models.assert_awaited_once_with(to_query_params(body.filters, 10, 2))
    assert gateway.list_openrouter_models.call_args.args[0]["q"] == "flux"


@pytest.mark.asyncio
async def test_browse_route_maps_gateway_error_to_code():
    gateway = SimpleNamespace(list_openrouter_models=AsyncMock(side_effect=ModelGatewayError("upstream returned HTTP 401")))
    result, _, _ = await _browse(OpenRouterBrowseRequest(api_key="k"), _config({}), gateway)
    assert (result.ok, result.error) == (False, "auth_error")


def test_browse_request_rejects_unknown_and_oversized():
    with pytest.raises(ValidationError):
        OpenRouterBrowseRequest.model_validate({"filters": {"nope": 1}})
    with pytest.raises(ValidationError):
        OpenRouterBrowseRequest(limit=201)
    with pytest.raises(ValidationError):
        OpenRouterBrowseRequest.model_validate({"x": 1})
