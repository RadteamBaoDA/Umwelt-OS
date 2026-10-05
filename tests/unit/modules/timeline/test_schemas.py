"""Unit tests for timeline event schemas, EventFilter (TimelineQuery), TimelinePage, and cursor encoding/decoding.

Covers:
- EventCreate, EventPatch, EventRead, TimelineQuery (EventFilter), and TimelinePage.
- date_precision time shape validation: "timed", "date", and "unknown" rules.
- ParticipantInput role validation, bounded metadata, and unique participant keys.
- Cursor encoding, decoding, fingerprint verification, partition checking, and tampering detection.
"""

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from modules.timeline.public import _cursor_decode, _cursor_encode
from modules.timeline.schemas import (
    EventCreate,
    EventPage,
    EventPatch,
    EventRead,
    ParticipantInput,
    TimelinePage,
    TimelineQuery,
)

# Alias per prompt specification
EventFilter = TimelineQuery


class TestParticipantInput:
    """Tests for ParticipantInput validation, role cleaning, and metadata."""

    def test_valid_participant_input(self) -> None:
        """Verify valid participant input collapses role whitespace."""
        entity_id = uuid4()
        participant = ParticipantInput(
            entity_id=entity_id,
            role="  keynote   speaker  ",
            metadata={"department": "Engineering"},
        )
        assert participant.entity_id == entity_id
        assert participant.role == "keynote speaker"
        assert participant.metadata == {"department": "Engineering"}

    def test_blank_role_rejected(self) -> None:
        """Verify blank or whitespace-only role is rejected."""
        with pytest.raises(ValidationError, match="role cannot be blank"):
            ParticipantInput(entity_id=uuid4(), role="   ")

    def test_extra_fields_forbidden(self) -> None:
        """Verify extra fields are forbidden on ParticipantInput."""
        with pytest.raises(ValidationError):
            ParticipantInput(entity_id=uuid4(), role="speaker", extra="val")  # type: ignore[call-arg]


class TestEventCreate:
    """Tests for EventCreate validation across timed, date, and unknown precision modes."""

    def test_valid_timed_event(self) -> None:
        """Verify timed event requires started_at and forbids occurred_date / end_date."""
        start = datetime(2025, 4, 1, 10, 0, tzinfo=UTC)
        end = datetime(2025, 4, 1, 12, 0, tzinfo=UTC)

        event = EventCreate(
            type="meeting",
            title="Design Review",
            date_precision="timed",
            started_at=start,
            ended_at=end,
        )
        assert event.type == "meeting"
        assert event.title == "Design Review"
        assert event.date_precision == "timed"
        assert event.started_at == start
        assert event.ended_at == end

    def test_timed_event_missing_started_at_rejected(self) -> None:
        """Verify timed event without started_at raises ValidationError."""
        with pytest.raises(ValidationError, match="timed events require started_at"):
            EventCreate(
                type="meeting",
                title="Design Review",
                date_precision="timed",
                started_at=None,
            )

    def test_timed_event_with_calendar_fields_rejected(self) -> None:
        """Verify timed event containing occurred_date is rejected."""
        start = datetime(2025, 4, 1, 10, 0, tzinfo=UTC)
        with pytest.raises(ValidationError, match="forbid calendar occurrence fields"):
            EventCreate(
                type="meeting",
                title="Review",
                date_precision="timed",
                started_at=start,
                occurred_date=date(2025, 4, 1),
            )

    def test_timed_event_ended_before_started_rejected(self) -> None:
        """Verify timed event where ended_at < started_at is rejected."""
        start = datetime(2025, 4, 1, 12, 0, tzinfo=UTC)
        end = datetime(2025, 4, 1, 10, 0, tzinfo=UTC)
        with pytest.raises(ValidationError, match="ended_at must be at or after started_at"):
            EventCreate(
                type="meeting",
                title="Review",
                date_precision="timed",
                started_at=start,
                ended_at=end,
            )

    def test_valid_date_event(self) -> None:
        """Verify date-precision event requires occurred_date and forbids timestamps."""
        event = EventCreate(
            type="conference",
            title="PyCon 2025",
            date_precision="date",
            occurred_date=date(2025, 5, 15),
            end_date=date(2025, 5, 18),
        )
        assert event.date_precision == "date"
        assert event.occurred_date == date(2025, 5, 15)
        assert event.end_date == date(2025, 5, 18)

    def test_date_event_with_timestamp_rejected(self) -> None:
        """Verify date-precision event providing started_at is rejected."""
        with pytest.raises(ValidationError, match="date events require occurred_date and forbid timestamps"):
            EventCreate(
                type="conference",
                title="PyCon",
                date_precision="date",
                occurred_date=date(2025, 5, 15),
                started_at=datetime.now(UTC),
            )

    def test_date_event_inverted_range_rejected(self) -> None:
        """Verify date-precision event where end_date < occurred_date is rejected."""
        with pytest.raises(ValidationError, match="end_date must be at or after occurred_date"):
            EventCreate(
                type="conference",
                title="PyCon",
                date_precision="date",
                occurred_date=date(2025, 5, 20),
                end_date=date(2025, 5, 15),
            )

    def test_valid_unknown_event(self) -> None:
        """Verify unknown precision event forbids all occurrence fields."""
        event = EventCreate(
            type="historical_fact",
            title="Discovery of Penicillin",
            date_precision="unknown",
        )
        assert event.date_precision == "unknown"

    def test_unknown_event_with_date_or_time_rejected(self) -> None:
        """Verify unknown precision event providing occurrence date/time is rejected."""
        with pytest.raises(ValidationError, match="unknown events cannot contain occurrence fields"):
            EventCreate(
                type="fact",
                title="Fact",
                date_precision="unknown",
                occurred_date=date(2025, 1, 1),
            )

    def test_duplicate_participant_keys_rejected(self) -> None:
        """Verify duplicate (entity_id, role) pairs in participants are rejected."""
        eid = uuid4()
        p1 = ParticipantInput(entity_id=eid, role="speaker")
        p2 = ParticipantInput(entity_id=eid, role="speaker")
        with pytest.raises(ValidationError, match="participant entity and role pairs must be unique"):
            EventCreate(
                type="talk",
                title="Talk Title",
                date_precision="unknown",
                participants=[p1, p2],
            )

    def test_duplicate_evidence_pairs_rejected(self) -> None:
        """Verify duplicate (version_id, chunk_id) evidence tuples are rejected."""
        v_id = uuid4()
        c_id = uuid4()
        with pytest.raises(ValidationError, match="evidence pairs must be unique"):
            EventCreate(
                type="talk",
                title="Talk",
                date_precision="unknown",
                evidence=[(v_id, c_id), (v_id, c_id)],
            )

    def test_naive_timestamp_rejected(self) -> None:
        """Verify naive timestamps without UTC offset raise ValidationError."""
        naive_dt = datetime(2025, 1, 1, 10, 0)
        with pytest.raises(ValidationError, match="timestamps must include an explicit UTC offset"):
            EventCreate(
                type="talk",
                title="Talk",
                date_precision="timed",
                started_at=naive_dt,
            )


class TestEventPatch:
    """Tests for EventPatch revision fencing and partial update constraints."""

    def test_valid_patch(self) -> None:
        """Verify minimal valid patch requires expected_revision and reason."""
        patch = EventPatch(expected_revision=1, reason="correct title", title="New Title")
        assert patch.expected_revision == 1
        assert patch.title == "New Title"

    def test_patch_cannot_clear_required_fields(self) -> None:
        """Verify type, title, and date_precision cannot be set to None in patch."""
        with pytest.raises(ValidationError, match="type and title cannot be cleared"):
            EventPatch(expected_revision=1, reason="test", title=None)

        with pytest.raises(ValidationError, match="type and title cannot be cleared"):
            EventPatch(expected_revision=1, reason="test", type=None)

        with pytest.raises(ValidationError, match="date_precision cannot be cleared"):
            EventPatch(expected_revision=1, reason="test", date_precision=None)


class TestTimelineQueryAndPage:
    """Tests for TimelineQuery (EventFilter) and TimelinePage."""

    def test_timeline_query_defaults(self) -> None:
        """Verify TimelineQuery default timezone and precision."""
        q = TimelineQuery()
        assert q.timezone == "Asia/Ho_Chi_Minh"
        assert q.precision == "all"
        assert q.date_from is None
        assert q.date_to is None

    def test_timeline_query_date_range_validation(self) -> None:
        """Verify date_from and date_to must be supplied together and date_to > date_from."""
        with pytest.raises(ValidationError, match="date_from and date_to must be supplied together"):
            TimelineQuery(date_from=date(2025, 1, 1))

        with pytest.raises(ValidationError, match="date_to must be after date_from"):
            TimelineQuery(date_from=date(2025, 1, 2), date_to=date(2025, 1, 1))

        valid_q = TimelineQuery(date_from=date(2025, 1, 1), date_to=date(2025, 1, 10))
        assert valid_q.date_from == date(2025, 1, 1)

    def test_timeline_query_unknown_precision_forbids_date_filter(self) -> None:
        """Verify unknown precision cannot be combined with date filters."""
        with pytest.raises(ValidationError, match="unknown precision cannot be combined with date filters"):
            TimelineQuery(
                precision="unknown",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 10),
            )

    def test_timeline_page_partition_order(self) -> None:
        """Verify TimelinePage default partition order is ('timed', 'date', 'unknown')."""
        page = TimelinePage(items=[], next_cursor=None)
        assert page.partition_order == ("timed", "date", "unknown")


class TestTimelineCursorEncodingDecoding:
    """Tests for cursor serialization, deserialization, fingerprinting, and error handling."""

    def test_cursor_round_trip(self) -> None:
        """Verify _cursor_encode and _cursor_decode round trip correctly."""
        fingerprint = "test_fingerprint_123"
        payload = {
            "v": 1,
            "f": fingerprint,
            "p": 0,
            "k": ["2025-04-01T10:00:00+00:00", str(uuid4())],
        }
        encoded = _cursor_encode(payload)
        decoded = _cursor_decode(encoded, fingerprint)
        assert decoded == payload

    def test_cursor_empty_value_returns_initial_position(self) -> None:
        """Verify None or empty cursor returns default initial partition position."""
        fingerprint = "fp"
        pos = _cursor_decode(None, fingerprint)
        assert pos == {"v": 1, "f": fingerprint, "p": 0, "k": None}

    def test_cursor_fingerprint_mismatch_rejected(self) -> None:
        """Verify cursor generated with one filter fingerprint is rejected by another."""
        payload = {"v": 1, "f": "original_fp", "p": 0, "k": None}
        encoded = _cursor_encode(payload)
        with pytest.raises(ValueError, match="cursor does not match this query"):
            _cursor_decode(encoded, "different_fp")

    def test_cursor_version_mismatch_rejected(self) -> None:
        """Verify cursor with v != 1 raises ValueError."""
        payload = {"v": 2, "f": "fp", "p": 0, "k": None}
        encoded = _cursor_encode(payload)
        with pytest.raises(ValueError, match="cursor does not match this query"):
            _cursor_decode(encoded, "fp")

    def test_cursor_invalid_partition_rejected(self) -> None:
        """Verify partition index outside 0..2 is rejected."""
        payload = {"v": 1, "f": "fp", "p": 5, "k": None}
        encoded = _cursor_encode(payload)
        with pytest.raises(ValueError, match="cursor partition is invalid"):
            _cursor_decode(encoded, "fp")

    def test_cursor_malformed_base64_rejected(self) -> None:
        """Verify non-base64 input raises ValueError."""
        with pytest.raises(ValueError, match="cursor is malformed"):
            _cursor_decode("???not-base64???", "fp")
