"""Chat reads the CURRENT configuration scope of active and paused sources only."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors.public import chat_scope_discriminators
from modules.connectors.service import scope as scope_module


@pytest.mark.asyncio
async def test_active_paused_included_archived_excluded_unsupported_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archived sources are omitted; an unverifiable scope maps to None; paused keeps its hash."""
    ws = uuid4()
    ids = {name: uuid4() for name in ("active", "paused", "archived", "generic", "missing")}

    def src(name: str, status: str, provider: str | None) -> SimpleNamespace:
        return SimpleNamespace(id=ids[name], status=status, provider=provider, type="rss")

    found = {
        ids["active"]: src("active", "active", "binance"),
        ids["paused"]: src("paused", "paused", "binance"),
        ids["archived"]: src("archived", "archived", "binance"),
        ids["generic"]: src("generic", "active", None),
    }
    seen: dict[str, object] = {}

    async def fake_batch(_session: object, source_ids: frozenset, **kwargs: object) -> dict:
        seen["ids"] = source_ids
        seen["kwargs"] = kwargs
        return found

    from modules.sources import public as sources

    monkeypatch.setattr(sources, "get_connector_sources", fake_batch)
    monkeypatch.setattr(
        scope_module,
        "_scope_snapshot",
        lambda s: SimpleNamespace(discriminator=f"hash-{s.status}") if s.provider else None,
    )
    out = await chat_scope_discriminators(
        object(),  # type: ignore[arg-type]
        list(ids.values()),
        scope=SimpleNamespace(workspace_id=ws),  # type: ignore[arg-type]
        multi_workspace_enabled=False,
    )
    assert out == {
        ids["active"]: "hash-active",
        ids["paused"]: "hash-paused",
        ids["generic"]: None,
    }
    assert ids["archived"] not in out and ids["missing"] not in out
    assert seen["ids"] == frozenset(ids.values())
