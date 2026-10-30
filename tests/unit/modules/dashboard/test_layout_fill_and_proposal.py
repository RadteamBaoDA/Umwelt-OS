"""Row fill for default desktop layouts and pure preset source proposal."""

from uuid import UUID, uuid4

import pytest

from modules.dashboard import gadgets, layouts


def test_rows_reach_column_edge_and_keep_minimums() -> None:
    """Each row's last rectangle is stretched to the edge; wider than minimum is allowed."""
    a, b, c = uuid4(), uuid4(), uuid4()
    sizes = {a: (8, 4), b: (8, 6), c: (8, 4)}
    items = layouts.default_desktop_layout(sizes, columns=20)
    by = {i.instance_id: i for i in items}
    assert (by[a].x, by[a].w) == (0, 8) and (by[b].x, by[b].w) == (8, 12)
    assert (by[c].x, by[c].y, by[c].w) == (0, 6, 20)


def test_single_rect_and_exact_fit_unchanged() -> None:
    """A rectangle already spanning the row is untouched; a lone narrow one fills the row."""
    a, b = uuid4(), uuid4()
    assert layouts.default_desktop_layout({a: (20, 3)})[0].w == 20
    assert layouts.default_desktop_layout({b: (5, 3)})[0].w == 20


def test_fill_still_rejects_oversized() -> None:
    """Validation on renderer size is unchanged."""
    with pytest.raises(ValueError):
        layouts.default_desktop_layout({uuid4(): (21, 3)})


def _ids(n: int) -> list[UUID]:
    return [UUID(int=i + 1) for i in range(n)]


def test_propose_none_and_single_and_multi() -> None:
    """none binds nothing, single one source, multi at most three, deterministic order."""
    ids = _ids(5)
    active = [(i, "rss", "rss") for i in reversed(ids)]
    assert gadgets.propose_slot_sources(next(r.id for r in gadgets.RENDERERS if r.source_mode == "none"), active) == []
    assert gadgets.propose_slot_sources("news_feed", active) == ids[:3]
    single = next(r.id for r in gadgets.RENDERERS if r.source_mode == "single")
    assert len(gadgets.propose_slot_sources(single, active)) <= 1


def test_propose_filters_by_shape() -> None:
    """Incompatible sources are never proposed; empty input yields empty."""
    assert gadgets.propose_slot_sources("news_feed", []) == []
    row = next(r for r in gadgets.RENDERERS if r.source_mode != "none" and r.accepted_shapes)
    for i, (prov, kind) in enumerate([("rss", "rss"), ("yahoo_finance", "market"), (None, "document")]):
        got = gadgets.propose_slot_sources(row.id, [(UUID(int=i + 1), prov, kind)])
        assert all(gadgets.source_data_shapes(prov, kind) & row.accepted_shapes for _ in got)


def test_propose_ranks_provider_sources_before_generic() -> None:
    """Provider-backed sources come before provider-less generic pages, regardless of id order."""
    generic, provided = UUID(int=1), UUID(int=2)
    active = [(generic, None, "web"), (provided, "bbc", "rss")]
    assert gadgets.propose_slot_sources("news_feed", active) == [provided, generic]
