"""Owner probe routes that call out to the network run under read admission, so each carries a request budget."""

import pytest

from modules.connectors.provisioning._routes.config import router


@pytest.mark.parametrize("suffix", ["/validate-draft", "/rest-sample", "/validate"])
def test_probe_route_has_connector_probe_budget(suffix: str) -> None:
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
    assert "connector_probe" in names
