"""Topic-page presets (news, markets, work): exact 20-column row tiling and renderer minimums."""

from uuid import UUID

import pytest

from modules.dashboard import gadgets, layouts
from modules.dashboard._presets import _preview_layouts
from modules.dashboard.schemas import LayoutItem

# preset id -> expected (x, y, w, h) per slot, in slot order.
EXPECTED = {
    "news": [(0, 0, 20, 8), (0, 8, 7, 10), (7, 8, 7, 10), (14, 8, 6, 10)],
    "markets": [
        (0, 0, 20, 3),
        (0, 3, 7, 6),
        (7, 3, 7, 6),
        (14, 3, 6, 6),
        (0, 9, 14, 8),
        (14, 9, 6, 8),
    ],
    "work": [(0, 0, 14, 8), (14, 0, 6, 8), (0, 8, 14, 7), (14, 8, 6, 7)],
}


def _place(preset_id: str) -> list[LayoutItem]:
    """Lay out a preset exactly as the preview does, with renderer minimums enforced."""
    preset = gadgets.dashboard_preset(preset_id)
    sizes = {UUID(int=i + 1): gadgets.preset_slot_size(s) for i, s in enumerate(preset.slots)}
    minimums = {
        UUID(int=i + 1): (
            gadgets.renderer_descriptor(s.renderer).minimum_width,
            gadgets.renderer_descriptor(s.renderer).minimum_height,
        )
        for i, s in enumerate(preset.slots)
    }
    return layouts.default_desktop_layout(sizes, minimum_sizes=minimums)


@pytest.mark.parametrize("preset_id", sorted(EXPECTED))
def test_topic_preset_tiles_exactly(preset_id: str) -> None:
    """Rows sum to 20 columns, nothing overlaps, and geometry matches the design."""
    assert gadgets.dashboard_preset(preset_id).family == "purpose"
    placed = _place(preset_id)
    assert [(i.x, i.y, i.w, i.h) for i in placed] == EXPECTED[preset_id]
    rows: dict[int, int] = {}
    for item in placed:
        rows[item.y] = rows.get(item.y, 0) + item.w
    assert set(rows.values()) == {20}
    for a in placed:
        for b in placed:
            if a is not b:
                assert not (
                    a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
                )


@pytest.mark.parametrize("preset_id", sorted(EXPECTED))
def test_topic_preset_sizes_meet_renderer_minimums(preset_id: str) -> None:
    """Requested slot sizes are never raised or below the renderer minimum."""
    for slot in gadgets.dashboard_preset(preset_id).slots:
        descriptor = gadgets.renderer_descriptor(slot.renderer)
        assert slot.size is not None
        assert slot.size[0] >= descriptor.minimum_width
        assert slot.size[1] >= descriptor.minimum_height


@pytest.mark.parametrize("preset_id", sorted(EXPECTED))
def test_topic_preset_preview_layouts_for_append(preset_id: str) -> None:
    """The preview layout (used by append mode) covers every slot on both breakpoints."""
    preset = gadgets.dashboard_preset(preset_id)
    slots = [{"slot_id": s.slot_id, "renderer": s.renderer} for s in preset.slots]
    result = _preview_layouts(preset, preset_id, slots)
    for key in result.values():
        assert {i["slot_id"] for i in key["items"]} == {s.slot_id for s in preset.slots}
