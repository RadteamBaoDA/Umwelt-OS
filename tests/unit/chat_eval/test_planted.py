"""Unit checks for the planted gold documents: the shipped fixture file and the pure loader."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.integration.chat_eval.planted import (
    PLANTED_PATH,
    PlantedItem,
    _records_for,
    external_id_for,
    load_planted,
)


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


def test_planted_external_id_matches_mapper_provider_id() -> None:
    """Each planted key's external_id equals the provider_id the mapper emits for its record.

    JSON-feed mappers prefix the provider (``emsc_quakes:quake_00``), so the lookup must use the same
    value the normalizer writes to ``documents.external_id``; the news mapper keeps the key as-is.
    """
    items = _items()
    produced: set[str] = set()
    for source_key in {i.source_key for i in items}:
        group = [i for i in items if i.source_key == source_key]
        produced.update(r.provider_id for r in _records_for(group, date(2026, 11, 16)))
    for item in items:
        assert external_id_for(item) in produced, item.key
    media = next(i for i in items if i.provider == "tvmaze_schedule")
    assert external_id_for(media) == f"tvmaze:{900000 + int(media.key.split('_')[1])}"


def test_media_rows_carry_release_date_attribute() -> None:
    """Media seed rows go through the production TVMaze mapper, so release_date lands in source_fields."""
    monday = date(2026, 11, 16)
    media = [i for i in _items() if i.provider == "tvmaze_schedule"]
    records = {r.provider_id: r for r in _records_for(media, monday)}
    assert len(records) == len(media) == 6
    for item in media:
        offset = int(item.attributes["release_week_offset_days"])
        fields = records[external_id_for(item)].metadata["provider_record"]["source_fields"]
        # The TVMaze mapper keeps the airstamp instant (UTC, ``Z``) as release_date, not a bare date.
        air_day = monday + timedelta(days=offset)
        assert fields["release_date"] == f"{air_day.isoformat()}T20:00:00Z", item.key
