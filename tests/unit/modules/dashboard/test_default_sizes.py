"""Default gadget sizes: >= minimum, roomy placement, and minimum fallback."""

from uuid import uuid4

from modules.dashboard import gadgets, layouts


def test_defaults_cover_every_renderer_and_respect_minimums() -> None:
    for descriptor in gadgets.RENDERERS:
        width, height = gadgets.renderer_default_size(descriptor.id)
        assert descriptor.minimum_width <= width <= 20
        assert height >= descriptor.minimum_height
        assert descriptor.id in gadgets.DEFAULT_SIZES


def test_news_feed_empty_dashboard_gets_default_at_origin() -> None:
    size = gadgets.renderer_default_size("news_feed")
    assert size == (6, 8)
    assert layouts.find_free_position([], 20, *size) == (0, 0)


def test_only_minimum_space_fits() -> None:
    occupied = [(0, 0, 17, 100)]  # 3 free columns; nothing fits at y < 100 except below
    descriptor = gadgets.renderer_descriptor("news_feed")
    assert layouts.find_free_position(occupied, 20, 6, 8) == (0, 100)
    assert layouts.find_free_position([(0, 0, 16, 100_000)], 20, descriptor.minimum_width, descriptor.minimum_height) == (16, 0)
    assert layouts.find_free_position([(0, 0, 16, 100_000)], 20, 6, 8) is None


def test_default_layout_uses_defaults_but_validates_minimums() -> None:
    a, b = uuid4(), uuid4()
    defaults = {a: (6, 8), b: (12, 9)}
    minimums = {a: (4, 4), b: (8, 6)}
    desktop = layouts.default_desktop_layout(defaults, minimum_sizes=minimums)
    assert [(i.w, i.h) for i in desktop] == [(6, 8), (14, 9)]  # last in row stretched to edge
    mobile = layouts.default_mobile_layout(defaults, minimum_sizes=minimums)
    assert [(i.w, i.h, i.y) for i in mobile] == [(20, 8, 0), (20, 9, 8)]
