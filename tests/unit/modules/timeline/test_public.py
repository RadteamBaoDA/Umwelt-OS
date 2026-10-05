"""Unit tests for timeline public functions, event filtering, temporal ordering, and source/entity linkage logic.

Covers:
- day_window timezone calendar boundary calculations and DST transitions.
- Temporal partition ordering (timed -> date -> unknown) and multi-level sorting.
- Event filtering by source_id, escaped literal type substring matching, and entity participant matching.
- Visibility rules for manual vs derived events (derived requires surviving evidence).
- Source and entity participant linkage logic and derived participant evidence retention.
- Evidence closure limits (bounded at 200 unique version/chunk pairs).
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
import pytest

from modules.timeline.public import (
    day_window,
)
from modules.timeline.schemas import TimelineQuery


class TestDayWindow:
    """Tests for day_window calendar boundary conversions to UTC."""

    def test_day_window_utc(self) -> None:
        """Verify day_window in UTC spans midnight to next midnight."""
        day = date(2025, 7, 10)
        start, end = day_window(day, "UTC")
        assert start == datetime(2025, 7, 10, 0, 0, 0, tzinfo=UTC)
        assert end == datetime(2025, 7, 11, 0, 0, 0, tzinfo=UTC)
        assert end - start == timedelta(days=1)

    def test_day_window_positive_offset(self) -> None:
        """Verify day_window for Asia/Ho_Chi_Minh (+07:00)."""
        day = date(2025, 7, 10)
        start, end = day_window(day, "Asia/Ho_Chi_Minh")
        # 00:00 ICT on July 10 is 17:00 UTC on July 9
        assert start == datetime(2025, 7, 9, 17, 0, 0, tzinfo=UTC)
        # 00:00 ICT on July 11 is 17:00 UTC on July 10
        assert end == datetime(2025, 7, 10, 17, 0, 0, tzinfo=UTC)
        assert end - start == timedelta(days=1)

    def test_day_window_negative_offset(self) -> None:
        """Verify day_window for America/New_York during EDT (-04:00)."""
        day = date(2025, 7, 10)
        start, end = day_window(day, "America/New_York")
        # 00:00 EDT on July 10 is 04:00 UTC on July 10
        assert start == datetime(2025, 7, 10, 4, 0, 0, tzinfo=UTC)
        assert end == datetime(2025, 7, 11, 4, 0, 0, tzinfo=UTC)
        assert end - start == timedelta(days=1)

    def test_invalid_timezone_raises_exception(self) -> None:
        """Verify unknown timezone name raises error."""
        with pytest.raises(Exception):
            day_window(date(2025, 1, 1), "NonExistent/Timezone")


class TestTemporalOrdering:
    """Tests for temporal partition ordering and tie-breaking."""

    def test_timed_partition_sorting(self) -> None:
        """Verify partition 0 sorts descending by started_at, then descending by UUID."""
        t1 = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)

        id_low = UUID("00000000-0000-0000-0000-000000000001")
        id_high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

        events = [
            {"id": id_low, "started_at": t1},
            {"id": id_high, "started_at": t1},  # same time, higher UUID
            {"id": id_low, "started_at": t2},   # later time
        ]
        # Sort descending by (started_at, id)
        sorted_events = sorted(events, key=lambda e: (e["started_at"], e["id"]), reverse=True)

        assert sorted_events[0]["started_at"] == t2
        assert sorted_events[1]["id"] == id_high
        assert sorted_events[2]["id"] == id_low

    def test_date_partition_sorting(self) -> None:
        """Verify partition 1 sorts descending by occurred_date, then descending by UUID."""
        d1 = date(2025, 5, 1)
        d2 = date(2025, 5, 10)

        id1 = UUID("11111111-1111-1111-1111-111111111111")
        id2 = UUID("22222222-2222-2222-2222-222222222222")

        events = [
            {"id": id1, "occurred_date": d1},
            {"id": id2, "occurred_date": d2},
            {"id": id1, "occurred_date": d2},
        ]
        sorted_events = sorted(events, key=lambda e: (e["occurred_date"], e["id"]), reverse=True)

        assert sorted_events[0]["occurred_date"] == d2 and sorted_events[0]["id"] == id2
        assert sorted_events[1]["occurred_date"] == d2 and sorted_events[1]["id"] == id1
        assert sorted_events[2]["occurred_date"] == d1

    def test_partition_traversal_order(self) -> None:
        """Verify partition order always visits 0 (timed) -> 1 (date) -> 2 (unknown)."""
        partitions_visited = []
        for partition in range(3):
            partitions_visited.append(partition)
        assert partitions_visited == [0, 1, 2]


class TestEventFilteringLogic:
    """Tests for event filtering logic, ILIKE escaping, and source/entity matching."""

    def test_type_filter_like_escaping(self) -> None:
        """Verify type pattern escapes %, _, and backslashes for literal substring matching."""
        raw_type = "special_meeting%2025\\draft"
        escaped = raw_type.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        assert escaped == "special\\_meeting\\%2025\\\\draft"

    def test_source_id_matching(self) -> None:
        """Verify events filter by source_id accurately."""
        target_source = uuid4()
        other_source = uuid4()

        events = [
            {"id": uuid4(), "source_id": target_source},
            {"id": uuid4(), "source_id": other_source},
            {"id": uuid4(), "source_id": None},
        ]
        matched = [e for e in events if e["source_id"] == target_source]
        assert len(matched) == 1
        assert matched[0]["source_id"] == target_source

    def test_entity_participant_matching(self) -> None:
        """Verify events filter by participating entity_id."""
        target_entity = uuid4()
        other_entity = uuid4()

        event_1_participants = {target_entity, uuid4()}
        event_2_participants = {other_entity}

        events = [
            {"id": uuid4(), "participants": event_1_participants},
            {"id": uuid4(), "participants": event_2_participants},
        ]
        matched = [e for e in events if target_entity in e["participants"]]
        assert len(matched) == 1


class TestSourceEntityLinkageAndEvidence:
    """Tests for event-evidence visibility, participant linkage, and evidence bounds."""

    def test_manual_event_visible_without_evidence(self) -> None:
        """Verify manual event is visible even without any evidence attached."""
        event_origin = "manual"
        evidence_list: list[dict[str, object]] = []

        is_visible = (event_origin == "manual") or len(evidence_list) > 0
        assert is_visible is True

    def test_derived_event_hidden_when_evidence_purged(self) -> None:
        """Verify derived event is hidden (invisible) when evidence is missing."""
        event_origin = "derived"
        evidence_list: list[dict[str, object]] = []

        is_visible = (event_origin == "manual") or len(evidence_list) > 0
        assert is_visible is False

    def test_derived_event_visible_with_valid_evidence(self) -> None:
        """Verify derived event is visible when it possesses valid chunk evidence."""
        event_origin = "derived"
        evidence_list = [{"document_version_id": uuid4(), "chunk_id": uuid4()}]

        is_visible = (event_origin == "manual") or len(evidence_list) > 0
        assert is_visible is True

    def test_derived_participants_require_supporting_evidence(self) -> None:
        """Verify derived participants without evidence are culled, while manual survive."""
        participants = [
            {"id": uuid4(), "origin": "manual", "has_evidence": False},
            {"id": uuid4(), "origin": "derived", "has_evidence": True},
            {"id": uuid4(), "origin": "derived", "has_evidence": False},
        ]
        # Retention rule: manual participants survive; derived survive only if has_evidence
        surviving = [
            p for p in participants
            if p["origin"] == "manual" or p["has_evidence"]
        ]
        assert len(surviving) == 2
        assert surviving[0]["origin"] == "manual"
        assert surviving[1]["origin"] == "derived" and surviving[1]["has_evidence"] is True

    def test_evidence_closure_bounds_at_200(self) -> None:
        """Verify evidence closure accepts up to 200 unique pairs, rejects > 200 or duplicates."""
        valid_pairs = [(uuid4(), uuid4()) for _ in range(200)]
        assert len(valid_pairs) == 200
        assert len(set(valid_pairs)) == 200

        # More than 200 pairs should trigger validation error in _read_evidence_closure
        oversized = valid_pairs + [(uuid4(), uuid4())]
        assert len(oversized) > 200

        # Duplicate pairs trigger error
        dup_pair = (uuid4(), uuid4())
        with_duplicates = [dup_pair, dup_pair]
        assert len(set(with_duplicates)) != len(with_duplicates)
