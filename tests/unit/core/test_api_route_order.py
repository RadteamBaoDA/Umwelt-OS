"""The composed API route table must be identical to the pre-descriptor hand-written registration.

Route order matters for matching (for example literal paths before ``/{id}`` captures), so
the fixture records the complete ordered ``(kind, path, methods, name)`` list. Regenerate it
only for an intentional routing change: ``python -m tests.unit.core.test_api_route_order``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "api_route_table.json"


def _flatten(routes: list[Any], prefix: str = "") -> list[list[Any]]:
    """Flatten (lazily) included routers depth-first, keeping declaration order and accumulating prefixes."""
    rows: list[list[Any]] = []
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            rows.extend(_flatten(original.routes, prefix + route.include_context.prefix))
            continue
        rows.append(
            [
                type(route).__name__,
                prefix + (getattr(route, "path", None) or ""),
                sorted(getattr(route, "methods", None) or []),
                getattr(route, "name", None),
            ]
        )
    return rows


def route_table(app: FastAPI) -> list[list[Any]]:
    """Return the ordered, flattened ``[kind, path, methods, name]`` table of ``app``."""
    return _flatten(list(app.routes))


def _build_app() -> FastAPI:
    """Create the application exactly as the existing route-gate test does."""
    from apps.api.main import create_app
    from core.config import Settings

    return create_app(
        Settings(
            public_origin="http://localhost:3000", csrf_signing_secret="test-csrf-signing-secret"
        )
    )


def test_route_table_matches_recorded_baseline() -> None:
    """Descriptor-driven registration keeps every route, method, name and the exact order."""
    expected = json.loads(FIXTURE.read_text())
    actual = route_table(_build_app())
    assert len(actual) == len(expected)
    assert actual == expected


if __name__ == "__main__":  # pragma: no cover - fixture regeneration helper
    FIXTURE.write_text(json.dumps(route_table(_build_app()), indent=0))
