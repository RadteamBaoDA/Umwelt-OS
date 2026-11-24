"""Unit checks for the snapshot metadata pure helpers: source keys, anchor time and meta shape."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.integration.chat_eval.snapshot_meta import TZ_NAME, anchor_utc, build_meta, source_key


def test_source_key_prefers_provider_then_strips_collect_prefix() -> None:
    assert source_key("open_meteo", "evalc-open_meteo") == "open_meteo"
    assert source_key(None, "evalc-web_search") == "web_search"
    assert source_key(None, "plain") == "plain"


def test_anchor_utc_is_the_latest_non_null_time() -> None:
    early = datetime(2026, 11, 20, 1, tzinfo=UTC)
    late = datetime(2026, 11, 23, 9, tzinfo=UTC)
    assert anchor_utc(None, early, late, None) == late


def test_anchor_utc_is_none_without_any_time() -> None:
    assert anchor_utc(None, None) is None


def test_build_meta_carries_anchor_timezone_and_sources() -> None:
    anchor = datetime(2026, 11, 23, 9, tzinfo=UTC)
    meta = build_meta(
        anchor=anchor,
        revision="0029_translation_live_event",
        sources={"open_meteo": "11111111-1111-1111-1111-111111111111"},
        stats={"open_meteo": {"documents": 3, "observations": 0}},
    )
    assert meta["anchor_utc"] == "2026-11-23T09:00:00+00:00"
    assert meta["tz"] == TZ_NAME == "Asia/Ho_Chi_Minh"
    assert meta["alembic_revision"] == "0029_translation_live_event"
    assert meta["sources"]["open_meteo"].startswith("11111111")
    assert meta["source_stats"]["open_meteo"]["documents"] == 3
