"""Wave C point fixes for generic REST/RSS/MCP collection (fakes only: no database, no network)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from modules.connectors import collection, n8n
from modules.connectors.providers import rest
from tests.unit.modules.connectors.test_collection import _capture, _rest_harness, attempt

NOW = datetime.now(UTC).replace(microsecond=0)


def _feed(items: list[tuple[str, datetime | None]]) -> bytes:
    """RSS body from ``(guid, pubDate or None)`` pairs."""
    rows = "".join(
        f"<item><guid>{g}</guid>"
        + (f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>" if d else "") + "</item>"
        for g, d in items)
    return f"<rss><channel>{rows}</channel></rss>".encode()


@pytest.mark.asyncio
async def test_undated_rss_items_do_not_move_the_cursor():
    dated = NOW - timedelta(hours=3)

    async def fetch(_url):
        return _feed([("a", dated), ("undated", None)])

    out = await n8n.read_rss("https://a.example/feed", None, fetch=fetch)
    assert len(out["records"]) == 2 and out["cursor_after"] == dated.isoformat()  # not "now"


@pytest.mark.asyncio
async def test_generic_overlap_is_a_small_skew_not_a_day():
    cursor = (NOW - timedelta(hours=1)).isoformat()

    async def fetch(_url):
        return _feed([("old", NOW - timedelta(hours=5)), ("edge", NOW - timedelta(hours=1, minutes=5))])

    out = await n8n.read_rss("https://a.example/feed", cursor, fetch=fetch)
    assert [r["provider_id"] for r in out["records"]] == ["edge"]  # 5h-old dropped, 5min skew kept
    body = {"items": [{"id": 1, "updated": (NOW - timedelta(hours=5)).isoformat()},
                      {"id": 2, "updated": (NOW - timedelta(hours=1, minutes=5)).isoformat()}]}
    cfg = SimpleNamespace(url="https://a.example/items", items_path="items", id_field="id", title_field=None,
                          content_field=None, updated_field="updated")

    async def rest_fetch(_url, **_kw):
        return rest.Fetched(httpx.Response(200, json=body).content)

    got = await rest.collect_rest(cfg, cursor, fetch=rest_fetch)
    assert [r["provider_id"] for r in got.records] == ["2"]


@pytest.mark.asyncio
async def test_rss_page_over_the_record_cap_degrades_instead_of_failing(monkeypatch):
    body = _feed([(f"g{i}", NOW - timedelta(minutes=700 - i)) for i in range(600)])
    state = collection.CollectionState(cursor=None)
    run, _sent, got = _rest_harness(monkeypatch, state, [rest.Fetched(body)])
    run.extras = {}
    await collection._run_rss(run)  # used to raise CollectionIncomplete forever
    assert len(got["records"]) == 500
    assert got["after"] == (NOW - timedelta(minutes=700 - 499)).isoformat()  # newest taken
    assert got["update"].continuation_state is None and got["update"].stats["truncated"] is True


def _mcp_run(monkeypatch, per_call: list[int | Exception]):
    """Run with ``len(per_call)`` MCP calls; ints yield that many records, exceptions fail the call."""
    from modules.connectors import mcp as mcp_collection
    from modules.tools import public as tools

    got = _capture(monkeypatch)
    calls = [SimpleNamespace(grant_id=i, arguments={"i": i}) for i in range(len(per_call))]
    monkeypatch.setattr(mcp_collection, "validate", lambda _s: SimpleNamespace(connection_id=1, calls=calls))
    it = iter(per_call)

    async def read(*_a, **kw):
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def state(_run):
        return collection.CollectionState(cursor=None)

    monkeypatch.setattr(tools, "read_collection_capability", read)
    monkeypatch.setattr(mcp_collection, "normalize", lambda n, _a, _t: [{"provider_id": f"{n}-{j}"} for j in range(n)] if isinstance(n, int) else [])
    monkeypatch.setattr(collection, "_state", state)
    run = SimpleNamespace(attempt=attempt(source_type="mcp"), ctx={"agent_mcp_runtime": object()}, extras={},
                          gate=lambda: None)
    return run, got


@pytest.mark.asyncio
async def test_mcp_records_beyond_the_cap_are_counted(monkeypatch):
    run, got = _mcp_run(monkeypatch, [400, 300])
    await collection._run_mcp(run)
    assert len(got["records"]) == 500 and got["update"].stats["backlog"] == 200


@pytest.mark.asyncio
async def test_mcp_failed_calls_are_counted_in_stats(monkeypatch):
    run, got = _mcp_run(monkeypatch, [3, RuntimeError("boom")])
    await collection._run_mcp(run)
    assert got["update"].stats["failed_calls"] == 1 and got["update"].stats["new"] == 3
