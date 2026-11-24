"""The eval harness builds its gadget note from the ``(ids, overflow)`` provider lookup (C1 regression guard)."""

from __future__ import annotations

from tests.integration.chat_eval.gadget_note import gadget_note_for_turn


def test_gadget_note_for_turn_lists_providers_from_lookup_tuple() -> None:
    """The lookup tuple is unpacked, so the ids reach the hint instead of the tuple itself."""
    note = gadget_note_for_turn({}, (["emsc_quakes", "usgs"], 0), True)
    assert "emsc_quakes" in note and "usgs" in note
    assert "more" not in note


def test_gadget_note_for_turn_keeps_overflow_in_hint() -> None:
    """The overflow count the cap cut is passed through, as the worker does."""
    note = gadget_note_for_turn({}, (["emsc_quakes"], 4), True)
    assert "…and 4 more" in note
