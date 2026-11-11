"""Settings connection probes call out to the network, so each carries the shared `model_probe` request budget."""

import pytest

from modules.settings.routes import router


@pytest.mark.parametrize("suffix", ["/ai/discover", "/web-search/test", "/n8n/test"])
def test_settings_probe_route_has_model_probe_budget(suffix: str) -> None:
    route = next(
        r
        for r in router.routes
        if getattr(r, "path", "").endswith(suffix) and "POST" in getattr(r, "methods", ())
    )
    names = [
        cell.cell_contents
        for dep in route.dependencies  # type: ignore[attr-defined]
        for cell in dep.dependency.__closure__ or ()
    ]
    assert "model_probe" in names
