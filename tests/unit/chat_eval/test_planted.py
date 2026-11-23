"""Unit checks for the planted gold documents: the shipped fixture file and the pure loader."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tests.integration.chat_eval.planted import PLANTED_PATH, PlantedItem, load_planted


def _items() -> list[PlantedItem]:
    """The shipped planted items (loaded once per test)."""
    return load_planted(PLANTED_PATH)


def test_planted_keys_are_unique() -> None:
    keys = [item.key for item in _items()]
    assert len(keys) == len(set(keys))


def test_planted_set_sizes_match_spec() -> None:
    counts = Counter(item.set for item in _items())
    assert counts["volume"] == 80
    assert counts["attribute"] == 60
    assert counts["injection"] == 1
    assert counts["local_only"] == 1


def test_volume_gold_count_and_attribute_magnitudes() -> None:
    items = _items()
    gold = [i for i in items if i.set == "volume" and i.attributes.get("gold") is True]
    assert len(gold) == 12
    quakes = [i for i in items if i.set == "attribute"]
    assert sum(1 for q in quakes if float(q.attributes["magnitude"]) >= 5) == 5


def test_load_planted_rejects_duplicate_keys(tmp_path: Path) -> None:
    row = {
        "key": "dup",
        "set": "injection",
        "provider": "vnexpress_business",
        "source_key": "fx_planted_news",
        "title": "t",
        "body": "b",
        "lang": "en",
        "published_at": "2026-11-16T10:00:00+07:00",
        "attributes": {},
    }
    path = tmp_path / "planted.json"
    path.write_text(json.dumps({"items": [row, dict(row)]}), encoding="utf-8")
    with pytest.raises(ValueError, match="dup"):
        load_planted(path)


def test_load_planted_rejects_missing_required_field(tmp_path: Path) -> None:
    path = tmp_path / "planted.json"
    path.write_text(json.dumps({"items": [{"key": "only-key"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_planted(path)
