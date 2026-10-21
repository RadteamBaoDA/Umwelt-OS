"""Model-calling routes must carry a require_budget dependency."""

import pytest

from modules.dashboard.routes import router as dashboard
from modules.model_gateway.routes import router as gateway
from modules.translations.routes import router as translations

CASES = [
    (dashboard, "/api/v1/briefs/generate"),
    (gateway, "/api/v1/settings/models/{role}/test"),
    (gateway, "/api/v1/settings/models/draft-test"),
    (translations, "/api/v1/translations/batches"),
]


@pytest.mark.parametrize(("router", "path"), CASES)
def test_route_has_budget(router, path) -> None:
    route = next(r for r in router.routes if r.path == path and "POST" in r.methods)
    assert any(d.dependency.__qualname__.startswith("require_budget") for d in route.dependencies)
