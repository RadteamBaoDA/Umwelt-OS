"""Gadget note for one eval turn, built from the production provider lookup (pure, no I/O)."""

from __future__ import annotations

from typing import Any

from modules.chat._worker.setup import _gadget_note


def gadget_note_for_turn(
    req_params: dict[str, Any], lookup: tuple[list[str], int], sql_on: bool
) -> str:
    """The gadget prompt note for an eval turn, from the ``(ids, overflow)`` provider lookup.

    ``lookup`` is exactly what :func:`modules.chat.db_query.active_collected_providers` returns; it is
    unpacked here and the overflow count is forwarded, as the worker's ``_db_query_providers`` path does, so
    the prompt hint says how many providers the 30-id cap cut. Pure: it builds a string and performs no I/O.
    """
    providers, overflow = lookup
    return _gadget_note(req_params, True, providers, sql_on, overflow=overflow)
