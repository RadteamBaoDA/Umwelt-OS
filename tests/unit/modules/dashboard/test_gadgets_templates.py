"""Template catalog and renderer datasource-compatibility rules."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.dashboard import gadgets, public


def test_validator_rules() -> None:
    """Wrong shape, extra single sources and sources on sourceless renderers are rejected."""
    gadgets.validate_renderer_sources("provider_observation", [("usgs", "api")])
    gadgets.validate_renderer_sources("news_feed", [("bbc_world", "rss"), (None, "rss"), ("hn_top", "api")])
    gadgets.validate_renderer_sources("tasks", [])
    with pytest.raises(ValueError, match="provider_observation"):
        gadgets.validate_renderer_sources("provider_observation", [("github", "api")])
    with pytest.raises(ValueError, match="at most one"):
        gadgets.validate_renderer_sources("weather", [("open_meteo", "api"), ("open_meteo", "api")])
    with pytest.raises(ValueError, match="does not accept"):
        gadgets.validate_renderer_sources("tasks", [("bbc_world", "rss")])


def test_catalog_read() -> None:
    """The public catalog exposes every template with its renderer's source mode and accepted shapes."""
    reads = {t.id: t for t in public.list_templates()}
    assert len(reads) == len(gadgets.TEMPLATES)
    assert reads["metric"].source_mode == "single"
    assert reads["metric"].accepted_shapes == ["measurement"]
    assert reads["highlights"].accepted_shapes is None
    renderers = {r.id: r for r in public.renderer_reads()}
    assert renderers["news_feed"].accepted_shapes == ["event", "news"]
    assert renderers["github_project"].accepted_shapes == ["project_activity"]


@pytest.mark.asyncio
async def test_selected_source_check_rejects_incompatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """The create/patch guard maps fetched source providers through the renderer rule."""
    async def fake(*_a: object, **_k: object) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(provider="bbc_world", type="rss"),)

    monkeypatch.setattr(public.sources, "get_gadget_sources", fake)
    await public._validate_selected_sources(None, "news_feed", [uuid4()], scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider_observation"):
        await public._validate_selected_sources(
            None, "provider_observation", [uuid4()], scope=None, multi_workspace_enabled=False)  # type: ignore[arg-type]


def test_watch_rules_accepts_any_sources() -> None:
    """watch_rules evaluates highlights over sources, so it is multi-source and shape-unconstrained."""
    gadgets.validate_renderer_sources("watch_rules", [("bbc_world", "rss"), ("hn_top", "api")])


def test_gadget_templates_route_is_cacheable_and_parses() -> None:
    """GET /api/v1/gadget-templates returns 200, Cache-Control private max-age and valid GadgetTemplateRead items."""
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
    assert "max-age=3600" in response.headers["cache-control"]
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
