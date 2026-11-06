"""Runner-level wave D behaviour: per-type request URLs, region filter, native resume and config fingerprint.

Fakes only (no database, no network): lease, state, fetch and accept are stubbed like the sibling runner tests.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from modules.connectors import collection
from modules.connectors import provider_options as po
from modules.connectors.providers import news, rest, world_data
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import IngestionRecord
from tests.unit.modules.connectors.p3_helpers import fixture
from tests.unit.modules.connectors.test_collection_incremental import T0, _run
from tests.unit.modules.connectors.test_free_provider_dispatch import Gate, make_run


def _geo_record(identity: str, lat: float, lon: float) -> IngestionRecord:
    """Measurement-shaped record carrying typed coordinates."""
    return IngestionRecord(
        provider_id=identity,
        content=identity,
        observed_at=T0,
        collected_at=T0,
        metadata={
            "title": identity,
            "provider_record": {
                "coverage": "returned_snapshot",
                "world_data": {"latitude": lat, "longitude": lon},
            },
        },
    )


@pytest.mark.asyncio
async def test_pure_run_sends_default_url_without_options_and_option_url_with_them(monkeypatch):
    seen: list[str] = []
    for configuration in ({}, {"usgs_min_magnitude": "2.5", "usgs_period": "week"}):
        gate = Gate({"*": rest.Fetched(b"{}")})
        run, _ = make_run("usgs", gate, monkeypatch, configuration=configuration)
        monkeypatch.setattr(
            world_data, "map_pure_provider_body", lambda *_a, **_k: [_geo_record("a", 1, 1)]
        )
        await collection._run_pure(run)
        seen.append(gate.calls[0][0])
    assert seen == [
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson",
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson",
    ]


@pytest.mark.asyncio
async def test_pure_run_region_filter_drops_records_outside_bbox(monkeypatch):
    gate = Gate({"*": rest.Fetched(b"{}")})
    run, accepted = make_run("usgs", gate, monkeypatch, configuration={"region": "vietnam"})
    monkeypatch.setattr(
        world_data,
        "map_pure_provider_body",
        lambda *_a, **_k: [_geo_record("hanoi", 21.0, 105.8), _geo_record("tokyo", 35.7, 139.7)],
    )
    await collection._run_pure(run)
    assert [r.provider_id for r in accepted[0][0]] == ["hanoi"]


@pytest.mark.asyncio
async def test_hn_list_and_fanout_follow_options_with_hard_cap_30(monkeypatch):
    listing = json.dumps(list(range(100, 160))).encode()
    story = fixture("hn_item_story.json")
    gate = Gate(
        {
            "https://hacker-news.firebaseio.com/v0/newstories.json": rest.Fetched(listing),
            "*": rest.Fetched(story),
        }
    )
    run, _ = make_run(
        "hn_top", gate, monkeypatch, configuration={"hn_list": "new", "max_items": 100}
    )
    await collection._run_hn(run)
    assert (
        gate.calls[0][0].endswith("/newstories.json") and len(gate.calls) == 31
    )  # 1 listing + hard cap 30
    small = Gate(
        {
            "https://hacker-news.firebaseio.com/v0/topstories.json": rest.Fetched(listing),
            "*": rest.Fetched(story),
        }
    )
    run, _ = make_run("hn_top", small, monkeypatch, configuration={"max_items": 3})
    await collection._run_hn(run)
    assert small.calls[0][0].endswith("/topstories.json") and len(small.calls) == 4


def _gdelt_run(monkeypatch, configuration, cursor, stored_fp, seen_urls):
    """Gdelt run whose stored state carries ``stored_fp``; the request URL is appended to ``seen_urls``."""
    run, accepted = _run("gdelt_economy", configuration, cursor, b'{"articles": []}', monkeypatch)

    async def state(_run):
        return SimpleNamespace(
            validators_revision=None, etag=None, last_modified=None, continuation_state=stored_fp
        )

    async def get_body(_run, request, **_kw):
        seen_urls.append(request.url)
        return SimpleNamespace(body=b"{}", etag=None, last_modified=None)

    monkeypatch.setattr(collection, "_state", state)
    monkeypatch.setattr(collection, "_get_body", get_body)
    monkeypatch.setattr(news, "map_news_body", lambda *_a, **_k: [])
    return run, accepted


@pytest.mark.asyncio
async def test_gdelt_run_uses_option_query_and_pushes_cursor_as_startdatetime(monkeypatch):
    urls: list[str] = []
    cursor = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    cfg = {"gdelt_query": "oil", "max_items": 9}
    run, _ = _gdelt_run(
        monkeypatch, cfg, cursor.isoformat(), po.native_fingerprint("gdelt_economy", cfg), urls
    )
    await collection._run_gdelt(run)
    expected = (cursor - timedelta(minutes=10)).strftime("%Y%m%d%H%M%S")
    assert urls[0].endswith(
        f"?query=oil&mode=artlist&format=json&maxrecords=9&startdatetime={expected}"
    )


@pytest.mark.asyncio
async def test_changed_native_param_resets_cursor_to_now_minus_window(monkeypatch):
    urls: list[str] = []
    old_cursor = (datetime.now(UTC) - timedelta(days=5)).replace(microsecond=0).isoformat()
    stored = po.native_fingerprint("gdelt_economy", {})  # stored under the default query
    run, accepted = _gdelt_run(monkeypatch, {"gdelt_query": "oil"}, old_cursor, stored, urls)
    before = datetime.now(UTC)
    await collection._run_gdelt(run)
    reset = datetime.fromisoformat(accepted[0]["cursor_after"])
    assert (
        before - timedelta(hours=24, minutes=1)
        <= reset
        <= datetime.now(UTC) - timedelta(hours=23, minutes=59)
    )
    # The request window starts 10 minutes before the cursor (clock skew), so compare the exact stamp:
    # comparing only the date failed between 00:00 and 00:10 UTC when the skew crossed midnight.
    assert (
        "startdatetime=" in urls[0]
        and (reset - timedelta(minutes=10)).strftime("%Y%m%d%H%M%S") in urls[0]
    )
    assert run.extras["native_fp"] == po.native_fingerprint("gdelt_economy", {"gdelt_query": "oil"})


@pytest.mark.asyncio
async def test_unchanged_fingerprint_keeps_cursor(monkeypatch):
    urls: list[str] = []
    cursor = (datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    cfg = {"gdelt_query": "oil"}
    run, accepted = _gdelt_run(
        monkeypatch, cfg, cursor, po.native_fingerprint("gdelt_economy", cfg), urls
    )
    await collection._run_gdelt(run)
    assert accepted[0]["cursor_after"] == cursor
    assert (datetime.fromisoformat(cursor) - timedelta(minutes=10)).strftime(
        "%Y%m%d%H%M%S"
    ) in urls[0]


@pytest.mark.asyncio
async def test_legacy_state_without_fingerprint_counts_as_default_params(monkeypatch):
    cursor = (datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    run, accepted = _gdelt_run(monkeypatch, {}, cursor, None, [])
    await collection._run_gdelt(run)
    assert accepted[0]["cursor_after"] == cursor  # default params, unknown stored fp: keep
    run, accepted = _gdelt_run(monkeypatch, {"gdelt_timespan": "7d"}, cursor, None, [])
    await collection._run_gdelt(run)
    assert accepted[0]["cursor_after"] != cursor  # non-default params, unknown stored fp: reset


@pytest.mark.asyncio
async def test_providers_without_native_params_never_read_state_for_fingerprint(monkeypatch):
    run, _ = _run("bbc_world", {}, None, b"<rss><channel></channel></rss>", monkeypatch)

    async def boom(_run):
        raise AssertionError("state must not be read")

    monkeypatch.setattr(collection, "_state", boom)
    assert await collection._resume_cursor(run, SimpleNamespace(cursor_before="c")) == "c"
    assert "native_fp" not in run.extras


@pytest.mark.asyncio
async def test_accept_native_persists_fingerprint_as_continuation_state(monkeypatch):
    run, _ = _run("gdelt_economy", {}, None, b"{}", monkeypatch)
    sent = {}

    async def fake_accept(_session, _batch, **kw):
        sent.update(kw)

    async def noop(*_a, **_k):
        return None

    monkeypatch.undo()  # the sibling helper stubs _accept_native; restore the real one for this test
    monkeypatch.setattr(ingestion, "accept_native_collection", fake_accept)
    monkeypatch.setattr(collection, "_publish_realtime_snapshot", noop)
    run.extras["native_fp"] = "n1:abc"
    run.gate = SimpleNamespace(captured_operation=None)
    await collection._accept_native(
        run, SimpleNamespace(token=uuid4(), cursor_before=None), [], "returned_snapshot", T0
    )
    assert sent["state_update"].continuation_state == "n1:abc"
