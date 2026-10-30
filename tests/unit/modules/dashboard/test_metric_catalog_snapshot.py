"""Keeps the web test fixtures of the gadget contracts (metric catalog, renderers, templates) identical to the backend."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modules.dashboard import public

FIXTURES = Path(__file__).resolve().parents[4] / "apps" / "web" / "src" / "test" / "fixtures"
SNAPSHOT = FIXTURES / "metric-catalog.json"


def _dump(payload: Any) -> str:
    """Serialise one public read exactly as stored in a snapshot (sorted keys, trailing newline)."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _render() -> str:
    """Serialise the public metric catalog read exactly as stored in the snapshot."""
    return _dump(public.metric_catalog_read())


def _models(rows: list[Any]) -> list[Any]:
    """Return the JSON form of response models, as the routes serialise them."""
    return [row.model_dump(mode="json") for row in rows]


SNAPSHOTS: dict[str, Callable[[], str]] = {
    "metric-catalog.json": _render,
    "gadget-renderers.json": lambda: _dump(_models(public.renderer_reads())),
    "gadget-templates.json": lambda: _dump(_models(public.list_templates())),
}


@pytest.mark.parametrize("name", sorted(SNAPSHOTS))
def test_web_fixture_matches_backend(name: str) -> None:
    """Fails with regeneration instructions when a web fixture drifts; UPDATE_SNAPSHOTS=1 rewrites it."""
    expected = SNAPSHOTS[name]()
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
    assert path.exists() and path.read_text(encoding="utf-8") == expected, (
        f"{path} is out of date with the backend read; regenerate with "
        "UPDATE_SNAPSHOTS=1 .venv/Scripts/python.exe -m pytest tests/unit/modules/dashboard/test_metric_catalog_snapshot.py"
    )
