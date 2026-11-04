"""The OpenRouter browse route has its own request-budget bucket, separate from the shared model probes."""

from modules.settings.routes import router


def test_browse_route_uses_its_own_budget_bucket() -> None:
    route = next(r for r in router.routes if getattr(r, "path", "").endswith("/ai/openrouter/models"))
    dependency = route.dependencies[0].dependency  # type: ignore[attr-defined]
    assert "openrouter_browse" in [cell.cell_contents for cell in dependency.__closure__ or ()]
