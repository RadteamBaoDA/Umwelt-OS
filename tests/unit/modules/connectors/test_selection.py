"""Unit tests for the pure incremental selection helper."""

from datetime import UTC, datetime, timedelta

from modules.connectors.selection import (
    SelectionFilters,
    fold,
    matches_keywords,
    select_incremental,
)
from modules.ingestion.schemas import IngestionRecord

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)


def _rec(pid: str, observed_at: datetime, basis: str = "provider_published", title: str = "t",
         modified: datetime | None = None) -> IngestionRecord:
    pr = {"timestamp_basis": basis, "provider_modified_at": modified.isoformat() if modified else None}
    return IngestionRecord(provider_id=pid, content=title, observed_at=observed_at,
                           metadata={"title": title, "provider_record": pr})


def test_fold_strips_vietnamese_diacritics_and_d_stroke():
    assert fold("Đà Nẵng – Giá vàng!") == ("da", "nang", "gia", "vang")


def test_keywords_match_whole_words_not_substrings():
    assert matches_keywords("Gà rán giá rẻ", ["ga"], None)
    assert not matches_keywords("New garage opens", ["ga"], None)
    assert not matches_keywords("Check your mail", ["AI"], None)
    assert matches_keywords("giá vàng hôm nay", ["gia vang"], None)
    assert not matches_keywords("vàng giá", ["gia vang"], None)


def test_exclude_wins_over_include():
    assert not matches_keywords("Quảng cáo giá vàng", ["gia vang"], ["quang cao"])


def test_oldest_first_cap_leaves_backlog_and_advances_cursor_to_last_taken():
    recs = [_rec(f"a{i}", T0 + timedelta(minutes=i)) for i in range(5)]
    s = select_incremental(recs, cursor=None, seen=lambda r: False, filters=SelectionFilters(max_items=2), now=NOW)
    assert [r.provider_id for r in s.taken] == ["a0", "a1"]
    assert s.backlog == 3 and s.cursor_after == (T0 + timedelta(minutes=1)).isoformat()


def test_seen_items_do_not_consume_the_cap():
    recs = [_rec(f"a{i}", T0) for i in range(10)] + [_rec("new", T0)]
    s = select_incremental(recs, cursor=T0.isoformat(), seen=lambda r: r.provider_id != "new",
                           filters=SelectionFilters(max_items=2), now=NOW)
    assert [r.provider_id for r in s.taken] == ["new"] and s.duplicates == 10


def test_cursor_is_inclusive_lower_bound_minus_skew():
    old, edge = _rec("old", T0 - timedelta(hours=1)), _rec("edge", T0 - timedelta(minutes=5))
    s = select_incremental([old, edge], cursor=T0.isoformat(), seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["edge"]


def test_edited_item_with_newer_modified_time_is_taken():
    edited = _rec("x", T0 + timedelta(hours=2), basis="provider_modified")
    s = select_incremental([edited], cursor=(T0 + timedelta(hours=1)).isoformat(), seen=lambda r: False,
                           filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["x"]


def test_edit_with_old_published_and_new_updated_is_taken():
    edited = _rec("x", T0 - timedelta(days=1), modified=T0 + timedelta(hours=2))
    s = select_incremental([edited], cursor=(T0 + timedelta(hours=1)).isoformat(), seen=lambda r: False,
                           filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["x"]


def test_undated_items_are_taken_last_and_never_move_cursor():
    s = select_incremental([_rec("u", NOW, basis="collection"), _rec("d", T0)], cursor=None,
                           seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert [r.provider_id for r in s.taken] == ["d", "u"] and s.cursor_after == T0.isoformat()


def test_no_records_taken_keeps_cursor():
    s = select_incremental([], cursor=T0.isoformat(), seen=lambda r: False, filters=SelectionFilters(), now=NOW)
    assert s.cursor_after == T0.isoformat() and s.taken == ()


def test_max_age_drops_old_dated_items_only():
    s = select_incremental([_rec("old", NOW - timedelta(days=10)), _rec("u", NOW, basis="collection")],
                           cursor=None, seen=lambda r: False, filters=SelectionFilters(max_age_days=7), now=NOW)
    assert [r.provider_id for r in s.taken] == ["u"]


def test_gap_reported_when_oldest_returned_is_newer_than_cursor():
    s = select_incremental([_rec("a", T0 + timedelta(days=2))], cursor=T0.isoformat(), seen=lambda r: False,
                           filters=SelectionFilters(), now=NOW)
    assert s.gap == (T0.isoformat(), (T0 + timedelta(days=2)).isoformat())


def test_keyword_filter_uses_title_and_content():
    s = select_incremental([_rec("a", T0, title="Giá vàng tăng"), _rec("b", T0, title="Bóng đá")], cursor=None,
                           seen=lambda r: False, filters=SelectionFilters(include=("gia vang",)), now=NOW)
    assert [r.provider_id for r in s.taken] == ["a"]


def test_cursor_never_regresses_when_seen_lookup_misses():
    # C1: everything unseen + cap 2, old items inside the skew window: cursor_after must stay >= old cursor.
    recs = [_rec("a", T0 - timedelta(minutes=8)), _rec("b", T0 - timedelta(minutes=5)), _rec("c", T0),
            _rec("new", T0 + timedelta(minutes=30))]
    sel = select_incremental(recs, cursor=T0.isoformat(), seen=lambda r: False,
                             filters=SelectionFilters(max_items=2), now=NOW)
    assert datetime.fromisoformat(sel.cursor_after) >= T0


def test_backlog_zero_cursor_advances_past_filter_rejected_records():
    # M4: all fresh records rejected by the keyword filter -> cursor still moves to the newest passing floor+dedup.
    recs = [_rec("a", T0 + timedelta(minutes=1), title="spam"), _rec("b", T0 + timedelta(minutes=2), title="spam")]
    sel = select_incremental(recs, cursor=T0.isoformat(), seen=lambda r: False,
                             filters=SelectionFilters(exclude=("spam",)), now=NOW)
    assert sel.taken == () and sel.backlog == 0
    assert sel.cursor_after == (T0 + timedelta(minutes=2)).isoformat()


def test_backlog_positive_cursor_stays_at_last_taken_not_rejected():
    recs = [_rec("a", T0 + timedelta(minutes=1)), _rec("b", T0 + timedelta(minutes=2)),
            _rec("c", T0 + timedelta(minutes=3), title="spam")]
    sel = select_incremental(recs, cursor=T0.isoformat(), seen=lambda r: False,
                             filters=SelectionFilters(max_items=1, exclude=("spam",)), now=NOW)
    assert sel.backlog == 1
    assert sel.cursor_after == (T0 + timedelta(minutes=1)).isoformat()
