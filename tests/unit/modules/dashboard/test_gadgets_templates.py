"""Template catalog and renderer datasource-compatibility rules."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors.provider_specs import get_provider_spec
from modules.dashboard import gadgets, public


def test_template_providers_subset_of_renderer_and_known() -> None:
    """Each template provider is allowed by its renderer and (for built-ins) a known provider spec."""
    for template in gadgets.TEMPLATES:
        descriptor = gadgets.renderer_descriptor(template.renderer)
        if descriptor.source_providers is not None:
            assert set(template.providers) <= set(descriptor.source_providers)
        if template.renderer in {"news_feed", "provider_observation"}:
            assert all(get_provider_spec(p) is not None for p in template.providers)


def test_validator_rules() -> None:
    """Wrong provider, extra single sources and sources on sourceless renderers are rejected."""
    gadgets.validate_renderer_sources("provider_observation", ["usgs"])
    gadgets.validate_renderer_sources("news_feed", ["bbc_world", None, "hn_top"])
    gadgets.validate_renderer_sources("tasks", [])
    with pytest.raises(ValueError, match="provider_observation.*alternative_me"):
        gadgets.validate_renderer_sources("provider_observation", ["github"])
    with pytest.raises(ValueError, match="at most one"):
        gadgets.validate_renderer_sources("weather", ["open_meteo", "open_meteo"])
    with pytest.raises(ValueError, match="does not accept"):
        gadgets.validate_renderer_sources("tasks", ["bbc_world"])


def test_catalog_read() -> None:
    """The public catalog exposes every template with its renderer's source mode."""
    reads = {t.id: t for t in public.list_templates()}
    assert len(reads) == len(gadgets.TEMPLATES)
    assert reads["fear_greed"].source_mode == "single"
    assert reads["fear_greed"].scope.metrics == ["fear_greed_index"]
    assert reads["world_news"].filters.limit == 25
    renderers = {r.id: r for r in public.renderer_reads()}
    assert renderers["news_feed"].source_providers is None
    assert renderers["github_project"].source_providers == ["github"]


@pytest.mark.asyncio
async def test_selected_source_check_rejects_incompatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """The create/patch guard maps fetched source providers through the renderer rule."""
    async def fake(*_a: object, **_k: object) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(provider="bbc_world"),)

    monkeypatch.setattr(public.sources, "get_gadget_sources", fake)
    await public._validate_selected_sources(None, "news_feed", [uuid4()], scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider_observation"):
        await public._validate_selected_sources(
            None, "provider_observation", [uuid4()], scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]


def test_watch_rules_accepts_sources_and_btc_template_pins_symbols() -> None:
    """watch_rules evaluates highlights over sources, so it is multi-source; BTC template pins symbols."""
    gadgets.validate_renderer_sources("watch_rules", ["bbc_world", "hn_top"])
    btc = next(t for t in gadgets.TEMPLATES if t.id == "btc_price")
    assert set(btc.scope["symbols"]) == {"BTC", "BTCUSDT"}


def test_gadget_templates_route_is_no_store_and_parses() -> None:
    """GET /api/v1/gadget-templates returns 200, Cache-Control no-store and valid GadgetTemplateRead items."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from modules.dashboard import routes
    from modules.dashboard.schemas import GadgetTemplateRead

    app = FastAPI()
    app.include_router(routes.router)
    for dep in routes.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    response = TestClient(app).get("/api/v1/gadget-templates")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    items = [GadgetTemplateRead.model_validate(item) for item in response.json()]
    assert len(items) == len(gadgets.TEMPLATES)


@pytest.mark.asyncio
async def test_preset_apply_validates_slot_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_preset runs the datasource check for every slot, rejecting incompatible slot sources."""
    preset = gadgets.PRESETS[0]
    slot = preset.slots[0]
    seen: list[tuple[str, int]] = []

    async def fake_validate(_s: object, renderer: str, ids: object, **_k: object) -> None:
        seen.append((renderer, len(list(ids))))  # type: ignore[call-overload]
        raise ValueError("incompatible")

    class Fence:
        pass

    async def fake_admit(*_a: object, **_k: object) -> Fence:
        return Fence()

    async def noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(public, "_admit", fake_admit)
    monkeypatch.setattr(public, "_actor", lambda _s: uuid4())
    monkeypatch.setattr(public, "_lock_owner_creation_quota", noop)
    monkeypatch.setattr(public, "_lock_selected_sources", noop)
    monkeypatch.setattr(public, "_validate_selected_sources", fake_validate)
    payload = SimpleNamespace(slot_sources={slot.slot_id: [uuid4()]}, target_dashboard_id=None)
    with pytest.raises(ValueError, match="incompatible"):
        await public.apply_preset(None, preset.id, payload, scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    assert seen[0][0] == slot.renderer
