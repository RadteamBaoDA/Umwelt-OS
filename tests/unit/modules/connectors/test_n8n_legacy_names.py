"""Legacy BBD-OS names and webhook paths must keep resolving beside the Umwelt-OS ones."""

from uuid import UUID

import pytest

from modules.connectors.n8n import (
    alternate_workflow_name,
    connector_workflow_names,
    find_workflows_by_name,
    workflow_webhook_path,
    workflow_webhook_paths,
)

SOURCE = UUID("11111111-1111-1111-1111-111111111111")


class _Api:
    """Fake n8n API that knows a fixed set of workflow names."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.calls: list[str] = []

    async def find_workflows(self, name: str) -> list[dict[str, str]]:
        """Return one match when the exact name is known."""
        self.calls.append(name)
        return [{"id": "wf", "name": name}] if name in self.known else []


def test_names_and_paths() -> None:
    """Current and legacy forms are exposed; new builds use the current path."""
    assert connector_workflow_names(SOURCE) == (
        f"Umwelt-OS connector {SOURCE}",
        f"BBD-OS connector {SOURCE}",
    )
    assert workflow_webhook_paths(SOURCE, "web") == (
        f"umwelt-collect-url-{SOURCE.hex}",
        f"bbd-collect-url-{SOURCE.hex}",
    )
    assert workflow_webhook_path(SOURCE, "rss") == f"umwelt-collect-rss-{SOURCE.hex}"
    assert alternate_workflow_name("other name") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [0, 1])
async def test_lookup_resolves_either_prefix(stored: int) -> None:
    """A workflow stored under either prefix is found from either name."""
    names = connector_workflow_names(SOURCE)
    for ask in names:
        api = _Api({names[stored]})
        assert [m["name"] for m in await find_workflows_by_name(api, ask)] == [names[stored]]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lookup_skips_alternate_when_exact_match() -> None:
    """The alternate name is not queried when the exact name matches."""
    current, _ = connector_workflow_names(SOURCE)
    api = _Api({current})
    await find_workflows_by_name(api, current)  # type: ignore[arg-type]
    assert api.calls == [current]
