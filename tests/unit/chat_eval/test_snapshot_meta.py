"""Unit checks for the snapshot metadata pure helpers: source keys, anchor time and meta shape."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.integration.chat_eval.snapshot_meta import (
    _DOC_STATS_SQL,
    _OBS_STATS_SQL,
    TZ_NAME,
    _merge_stats,
    anchor_utc,
    build_meta,
    source_key,
)


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


def _stat_row(source_id: str, name: str, provider: str | None, n: int) -> SimpleNamespace:
    """A fake aggregate row shaped like the snapshot SQL output (test helper)."""
    when = datetime(2026, 11, 23, 9, tzinfo=UTC)
    return SimpleNamespace(
        source_id=source_id, name=name, provider=provider, n=n, first_at=when, last_at=when
    )


def test_stats_sql_reads_every_non_local_only_source_not_only_evalc_prefix() -> None:
    for sql in (_DOC_STATS_SQL, _OBS_STATS_SQL):
        assert "evalc" not in sql
        assert "s.local_only IS FALSE" in sql


def test_merge_stats_keeps_builtin_sources_named_by_provider() -> None:
    docs = [_stat_row("aaa", "bbc_world", "bbc_world", 4)]
    obs = [_stat_row("bbb", "emsc_quakes", None, 2)]
    sources, stats, _seen = _merge_stats(docs, [*obs])
    assert sources == {"bbc_world": "aaa", "emsc_quakes": "bbb"}
    assert stats["bbc_world"]["documents"] == 4
    assert stats["emsc_quakes"]["observations"] == 2


def test_merge_stats_raises_when_two_sources_share_a_source_key() -> None:
    docs = [_stat_row("aaa", "evalc-open_meteo", "open_meteo", 1)]
    obs = [_stat_row("bbb", "open_meteo", "open_meteo", 1)]
    with pytest.raises(ValueError, match="open_meteo"):
        _merge_stats(docs, obs)
