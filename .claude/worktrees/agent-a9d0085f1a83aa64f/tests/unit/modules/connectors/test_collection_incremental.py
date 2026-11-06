"""Runner-level incremental selection for built-in feeds (fakes only: no database, no network)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from modules.connectors import collection
from modules.connectors.provider_specs import INCREMENTAL_PROVIDERS
from modules.connectors.providers.json_feed import JSON_FEEDS
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import IngestionRecord
from tests.unit.modules.connectors.test_collection import attempt, factory

T0 = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)


def _rss(items: list[tuple[str, datetime]]) -> bytes:
    """Build an RSS body from ``(guid, pubDate)`` pairs."""
    rows = "".join(
        f"<item><title>title {g}</title><guid>{g}</guid><pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>"
        for g, d in items
    )
    return f"<rss><channel>{rows}</channel></rss>".encode()


def _run(
    provider: str, configuration: dict, cursor: str | None, body: bytes, monkeypatch, seen=None
):
    """Build a Run for ``provider`` with stubbed lease/fetch/seen lookups; return it with the captured accepts."""
    att = attempt(provider=provider)
    att.source.configuration = configuration
    run = collection.Run(ctx={}, factory=factory([]), settings=None, attempt=att, gate=None)
    accepted: list[dict] = []

    async def lease(_run):
        return SimpleNamespace(token="t", cursor_before=cursor)

    async def state(_run):
        return SimpleNamespace(validators_revision=None, etag=None, last_modified=None)

    async def get_body(_run, _request, **_kw):
        return SimpleNamespace(body=body, etag=None, last_modified=None)

    async def accept(_run, _lease, records, coverage, collected_at, state_update=None, **kw):
        accepted.append({"ids": [r.provider_id for r in records], "coverage": coverage, **kw})

    async def seen_keys(_session, **kw):
        keys = list(kw["keys"])
        return seen(keys) if seen else set()

    monkeypatch.setattr(collection, "_lease", lease)
    monkeypatch.setattr(collection, "_state", state)
    monkeypatch.setattr(collection, "_get_body", get_body)
    monkeypatch.setattr(collection, "_accept_native", accept)
    monkeypatch.setattr(ingestion, "seen_record_keys", seen_keys)
    return run, accepted


@pytest.mark.asyncio
async def test_feed_run_passes_cursor_after_and_caps_oldest_first(monkeypatch):
    body = _rss([(f"g{i}", T0 + timedelta(minutes=i)) for i in range(5)])
    run, accepted = _run(
        "bbc_world", {"max_items": 2}, (T0 - timedelta(hours=1)).isoformat(), body, monkeypatch
    )
    await collection._run_feed(run)
    assert accepted[0]["ids"] == ["g0", "g1"]
    assert accepted[0]["cursor_after"] == (T0 + timedelta(minutes=1)).isoformat()
    assert run.extras["selection"]["backlog"] == 3


@pytest.mark.asyncio
async def test_resume_after_generation_bump_only_reobserves_skew_window(monkeypatch):
    body = _rss([("g0", T0), ("g1", T0 + timedelta(hours=1)), ("g2", T0 + timedelta(hours=2))])
    run, accepted = _run("bbc_world", {}, (T0 + timedelta(hours=2)).isoformat(), body, monkeypatch)
    await collection._run_feed(run)  # seen lookup returns nothing: a fresh generation
    assert accepted[0]["ids"] == ["g2"]
    assert accepted[0]["cursor_after"] == (T0 + timedelta(hours=2)).isoformat()


@pytest.mark.asyncio
async def test_measurement_provider_is_untouched(monkeypatch):
    run, _ = _run("binance", {"max_items": 1}, T0.isoformat(), b"", monkeypatch)
    records = [IngestionRecord(provider_id=f"p{i}", content="c", observed_at=T0) for i in range(3)]
    assert await collection._select(
        run, SimpleNamespace(cursor_before=T0.isoformat()), records, T0
    ) == (records, None)


@pytest.mark.asyncio
async def test_cap_smaller_than_seen_run_still_takes_new_item(monkeypatch):
    body = _rss([(f"g{i}", T0) for i in range(10)] + [("new", T0)])
    run, accepted = _run(
        "bbc_world",
        {"max_items": 1},
        T0.isoformat(),
        body,
        monkeypatch,
        seen=lambda keys: {k for k in keys if k[0] != "new"},
    )
    await collection._run_feed(run)
    assert accepted[0]["ids"] == ["new"]
    assert run.extras["selection"]["duplicates"] == 10


@pytest.mark.asyncio
async def test_no_option_run_accepts_everything_and_keeps_old_cursor_when_nothing_dated(
    monkeypatch,
):
    body = _rss([("g0", T0), ("g1", T0 + timedelta(minutes=1))])
    run, accepted = _run("bbc_world", {}, None, body, monkeypatch)
    await collection._run_feed(run)
    assert sorted(accepted[0]["ids"]) == ["g0", "g1"]
    assert accepted[0]["cursor_after"] == (T0 + timedelta(minutes=1)).isoformat()


def test_accept_path_uses_native_record_key():
    rec = IngestionRecord(
        provider_id="p",
        content="c",
        observed_at=T0,
        version="v",
        metadata={"title": "t"},
        collected_at=T0,
    )
    expected = ingestion._digest(
        {
            "version": "v",
            "content": "c",
            "metadata": rec.model_dump(mode="json", exclude={"collected_at"})["metadata"],
        }
    )
    assert ingestion.native_record_key(rec) == ("p", expected)
    assert ingestion.native_record_key(rec.model_copy(update={"collected_at": None})) == (
        "p",
        expected,
    )


def test_every_json_feed_provider_is_selected_incrementally():
    # map_json_feed no longer caps; the runner's selection is the only cap, so no feed may bypass it.
    assert set(JSON_FEEDS) <= INCREMENTAL_PROVIDERS


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["hn_top", "google_news"])
async def test_ranking_feeds_are_dedup_only(provider, monkeypatch):
    # I1: an unseen item older than the stored cursor is still taken; the cursor is left unchanged.
    run, _ = _run(provider, {}, T0.isoformat(), b"", monkeypatch)
    old = IngestionRecord(
        provider_id="old", content="c", observed_at=T0 - timedelta(hours=5), metadata={"title": "t"}
    )
    taken, cursor_after = await collection._select(
        run, SimpleNamespace(cursor_before=T0.isoformat()), [old], T0
    )
    assert [r.provider_id for r in taken] == ["old"] and cursor_after is None


@pytest.mark.asyncio
async def test_gdelt_run_honours_max_items_and_cursor(monkeypatch):
    # I2: _run_gdelt goes through _select like the other runners.
    from modules.connectors.providers import news

    recs = [
        IngestionRecord(
            provider_id=f"g{i}",
            content="c",
            observed_at=T0 + timedelta(minutes=i),
            metadata={"title": f"t{i}", "provider_record": {"coverage": "returned_snapshot"}},
        )
        for i in range(4)
    ]
    monkeypatch.setattr(news, "map_news_body", lambda *_a, **_k: recs)
    run, accepted = _run(
        "gdelt_economy", {"max_items": 2}, (T0 - timedelta(hours=1)).isoformat(), b"{}", monkeypatch
    )
    await collection._run_gdelt(run)
    assert accepted[0]["ids"] == ["g0", "g1"]
    assert accepted[0]["cursor_after"] == (T0 + timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
async def test_realtime_preview_is_newest_first_from_full_mapped_list(monkeypatch):
    # M2: capped oldest-first accept must not make the preview show the oldest items.
    sets: list[str] = []

    class Redis:
        async def set(self, _key, payload, ex=None):
            sets.append(payload)

    recs = [
        IngestionRecord(
            provider_id=f"g{i}",
            content="c",
            observed_at=T0 + timedelta(minutes=i),
            metadata={"title": f"t{i}"},
        )
        for i in range(4)
    ]
    run, _ = _run(
        "bbc_world", {"max_items": 1}, (T0 - timedelta(hours=1)).isoformat(), b"", monkeypatch
    )
    run.ctx = {"redis": Redis()}
    taken, _ = await collection._select(
        run, SimpleNamespace(cursor_before=(T0 - timedelta(hours=1)).isoformat()), recs, T0
    )
    assert [r.provider_id for r in taken] == ["g0"]
    await collection._publish_realtime_snapshot(run, taken, T0)
    import json

    items = json.loads(sets[0])["items"]
    assert [i["provider_id"] for i in items] == ["g3", "g2", "g1", "g0"]
