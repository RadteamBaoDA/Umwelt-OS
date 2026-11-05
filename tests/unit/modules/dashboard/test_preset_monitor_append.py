"""Monitor preset layout, provider-preferring proposals and append-mode request validation."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from modules.dashboard import gadgets, layouts
from modules.dashboard.schemas import PresetApplyRequest

FINGERPRINT = "0" * 64


def test_monitor_rows_fill_the_grid() -> None:
    """Every Monitor row packs to exactly 20 columns, with the map and live news side by side first."""
    preset = gadgets.dashboard_preset("monitor")
    sizes = {
        UUID(int=index + 1): gadgets.preset_slot_size(slot)
        for index, slot in enumerate(preset.slots)
    }
    placed = layouts.default_desktop_layout(sizes)
    rows: dict[int, int] = {}
    for item in placed:
        rows[item.y] = rows.get(item.y, 0) + item.w
    assert set(rows.values()) == {20}
    first = sorted((item for item in placed if item.y == 0), key=lambda item: item.x)
    assert [preset.slots[item.instance_id.int - 1].slot_id for item in first] == [
        "situation_map",
        "live_news",
    ]
    assert len(preset.slots) <= 16  # PresetPreviewRequest.slot_sources bound


def test_provider_preference_is_strict_and_ordered() -> None:
    """A themed slot binds only its providers, in preference order, and stays unbound without them."""
    hn, bbc, tuoitre = UUID(int=1), UUID(int=2), UUID(int=3)
    active = [(hn, "hn_top", "api"), (bbc, "bbc_world", "rss"), (tuoitre, "tuoitre_news", "rss")]
    assert gadgets.propose_slot_sources(
        "news_feed", active, providers=("tuoitre_news", "bbc_world")
    ) == [tuoitre, bbc]
    assert gadgets.propose_slot_sources("news_feed", active, providers=("cisa_kev",)) == []
    assert gadgets.propose_slot_sources("news_feed", active) == [bbc, hn, tuoitre]


def test_append_requires_target_and_revision_without_confirmation() -> None:
    """Append is non-destructive: it needs a target and revision but rejects the replace confirmation."""
    target = UUID(int=9)
    ok = PresetApplyRequest(
        preview_fingerprint=FINGERPRINT,
        mode="append",
        target_dashboard_id=target,
        expected_revision=1,
    )
    assert ok.mode == "append"
    with pytest.raises(ValidationError):
        PresetApplyRequest(
            preview_fingerprint=FINGERPRINT, mode="append", target_dashboard_id=target
        )
    with pytest.raises(ValidationError):
        PresetApplyRequest(
            preview_fingerprint=FINGERPRINT,
            mode="append",
            target_dashboard_id=target,
            expected_revision=1,
            replace_confirmed=True,
        )


def test_monitor_map_slot_is_live_and_rejects_sources() -> None:
    """The situation map is a live slot, so explicit sources on it are rejected."""
    slot = next(
        s for s in gadgets.dashboard_preset("monitor").slots if s.slot_id == "situation_map"
    )
    assert gadgets.preset_slot_is_live(slot)
    assert "usgs" in gadgets.LIVE_MAP_DEFAULT_PROVIDERS
    with pytest.raises(ValueError, match="live map"):
        gadgets.validate_preset_slot_sources("monitor", {"situation_map": [UUID(int=1)]})
