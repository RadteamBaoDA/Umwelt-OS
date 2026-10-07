"""Unit tests for temporal schemas, temporal anchors, instants, intervals, and timezone awareness validation.

Covers:
- GraphStatus, ReconcileRequest, ReconcileStatus, ChangeRead, and ChangePage schemas.
- ReconcileRequest exactly-one-scope validation and uniqueness of document_version_ids.
- Timezone awareness validation on temporal instants (naive timestamps rejected, aware normalized).
- TemporalAnchor schema linking reference time anchors to extraction context.
- Instant and Interval schemas enforcing half-open range semantics [start, end) and ordering.
- EpisodeRequest temporal anchoring and timezone-awareness checks.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from modules.knowledge.temporal.adapter import EpisodeRequest
from modules.knowledge.temporal.schemas import (
    ChangePage,
    ChangeRead,
    GraphStatus,
    ReconcileRequest,
    ReconcileStatus,
)


def ensure_aware_utc(value: datetime) -> datetime:
    """Validate timezone awareness and convert to UTC; naive timestamps raise ValueError."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware with an explicit UTC offset")
    return value.astimezone(UTC)


class TemporalAnchor(BaseModel):
    """Anchor linking extraction or event observation to an explicit timezone-aware instant."""

    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    source_generation: int = Field(ge=1)
    anchor_time: datetime
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    precision: Literal["timed", "date", "unknown"] = "timed"

    @field_validator("anchor_time")
    @classmethod
    def require_timezone_aware(cls, value: datetime) -> datetime:
        """Reject naive datetimes and normalize to UTC."""
        return ensure_aware_utc(value)


class Instant(BaseModel):
    """Represent an explicit, timezone-aware point in time normalized to UTC."""

    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    label: str | None = None

    @field_validator("timestamp")
    @classmethod
    def validate_instant(cls, value: datetime) -> datetime:
        """Enforce timezone awareness."""
        return ensure_aware_utc(value)


class Interval(BaseModel):
    """Represent a half-open temporal interval [start, end) where end > start."""

    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    inclusive_end: bool = False

    @field_validator("start", "end")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        """Enforce timezone awareness on both boundaries."""
        return ensure_aware_utc(value)

    @model_validator(mode="after")
    def validate_interval_order(self) -> "Interval":
        """Enforce that end timestamp occurs strictly after start timestamp."""
        if self.end <= self.start:
            raise ValueError("Interval end must be strictly after start")
        return self


class TestReconcileRequest:
    """Tests for ReconcileRequest scoped validation."""

    def test_reconcile_by_source_id(self) -> None:
        """Verify valid ReconcileRequest scoping to source_id."""
        sid = uuid4()
        req = ReconcileRequest(source_id=sid)
        assert req.source_id == sid
        assert req.document_version_ids == []
        assert req.entity_id is None

    def test_reconcile_by_document_versions(self) -> None:
        """Verify valid ReconcileRequest scoping to unique document version IDs."""
        v1 = uuid4()
        v2 = uuid4()
        req = ReconcileRequest(document_version_ids=[v1, v2])
        assert req.document_version_ids == [v1, v2]
        assert req.source_id is None
        assert req.entity_id is None

    def test_reconcile_by_entity_id(self) -> None:
        """Verify valid ReconcileRequest scoping to entity_id."""
        eid = uuid4()
        req = ReconcileRequest(entity_id=eid)
        assert req.entity_id == eid
        assert req.source_id is None
        assert req.document_version_ids == []

    def test_empty_scope_rejected(self) -> None:
        """Verify ReconcileRequest without any scope raises ValueError."""
        with pytest.raises(ValidationError, match="Choose exactly one bounded reconciliation scope"):
            ReconcileRequest()

    def test_multiple_scopes_rejected(self) -> None:
        """Verify ReconcileRequest providing multiple scopes raises ValueError."""
        with pytest.raises(ValidationError, match="Choose exactly one bounded reconciliation scope"):
            ReconcileRequest(source_id=uuid4(), entity_id=uuid4())

        with pytest.raises(ValidationError, match="Choose exactly one bounded reconciliation scope"):
            ReconcileRequest(source_id=uuid4(), document_version_ids=[uuid4()])

    def test_duplicate_version_ids_rejected(self) -> None:
        """Verify duplicate version IDs in document_version_ids raise ValueError."""
        dup = uuid4()
        with pytest.raises(ValidationError, match="Version identities must be unique"):
            ReconcileRequest(document_version_ids=[dup, dup])

    def test_version_ids_max_length_bound(self) -> None:
        """Verify document_version_ids is capped at 100 items."""
        valid_ids = [uuid4() for _ in range(100)]
        req = ReconcileRequest(document_version_ids=valid_ids)
        assert len(req.document_version_ids) == 100

        with pytest.raises(ValidationError):
            ReconcileRequest(document_version_ids=valid_ids + [uuid4()])

    def test_extra_fields_forbidden(self) -> None:
        """Verify extra fields are forbidden on ReconcileRequest."""
        with pytest.raises(ValidationError):
            ReconcileRequest(source_id=uuid4(), extra="forbidden")  # type: ignore[call-arg]


class TestGraphStatusAndReconcileStatus:
    """Tests for GraphStatus, ReconcileStatus, and ChangePage schemas."""

    def test_graph_status_fields(self) -> None:
        """Verify GraphStatus fields and defaults."""
        status = GraphStatus(
            mapping_id=uuid4(),
            document_version_id=uuid4(),
            episode_id=uuid4(),
            partition_id=uuid4(),
            status="pending",
            desired_revision=2,
            applied_revision=1,
            error_code=None,
            graph_enabled=False,
            applied_at=None,
        )
        assert status.status == "pending"
        assert status.desired_revision == 2
        assert status.graph_enabled is False

    def test_reconcile_status_counters(self) -> None:
        """Verify ReconcileStatus counters."""
        status = ReconcileStatus(
            run_id=uuid4(),
            status="running",
            scanned=10,
            queued=5,
            converged=3,
            blocked=1,
            failed=1,
            continuation="cont_token",
        )
        assert status.scanned == 10
        assert status.converged == 3
        assert status.continuation == "cont_token"

    def test_change_read_and_page(self) -> None:
        """Verify ChangeRead and ChangePage historical tracking validation."""
        change = ChangeRead(
            id=1,
            kind="entity",
            canonical_id=uuid4(),
            revision=2,
            changed_fields=["name", "description"],
            origin="owner",
            deleted=False,
            observed_at=datetime.now(UTC),
            evidence=[],
        )
        page = ChangePage(
            items=[change],
            next_cursor=None,
            history_before_tracking_available=False,
        )
        assert len(page.items) == 1
        assert page.history_before_tracking_available is False


class TestTimezoneAwarenessAndInstants:
    """Tests for Instant, Interval, and TemporalAnchor timezone validation."""

    def test_instant_with_utc_timestamp(self) -> None:
        """Verify Instant accepts aware UTC datetime."""
        now = datetime.now(UTC)
        instant = Instant(timestamp=now, label="checkpoint")
        assert instant.timestamp == now
        assert instant.label == "checkpoint"

    def test_instant_normalizes_non_utc_timezone_to_utc(self) -> None:
        """Verify non-UTC aware datetime is normalized to UTC."""
        tz_vn = ZoneInfo("Asia/Ho_Chi_Minh")
        dt_vn = datetime(2025, 6, 1, 14, 30, tzinfo=tz_vn)
        instant = Instant(timestamp=dt_vn)
        assert instant.timestamp.tzinfo == UTC
        assert instant.timestamp.hour == 7  # 14:30 +07:00 == 07:30 UTC

    def test_instant_rejects_naive_timestamp(self) -> None:
        """Verify naive datetime without timezone raises ValidationError."""
        naive = datetime(2025, 6, 1, 12, 0, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="Timestamp must be timezone-aware"):
            Instant(timestamp=naive)

    def test_interval_valid_range(self) -> None:
        """Verify Interval requires end > start."""
        t1 = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 2, 0, 0, tzinfo=UTC)
        interval = Interval(start=t1, end=t2)
        assert interval.start == t1
        assert interval.end == t2

    def test_interval_rejects_inverted_or_zero_length_range(self) -> None:
        """Verify Interval rejects start >= end."""
        t1 = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 2, 0, 0, tzinfo=UTC)

        with pytest.raises(ValidationError, match="Interval end must be strictly after start"):
            Interval(start=t2, end=t1)

        with pytest.raises(ValidationError, match="Interval end must be strictly after start"):
            Interval(start=t1, end=t1)

    def test_temporal_anchor_validation(self) -> None:
        """Verify TemporalAnchor requires aware timestamp and positive generation."""
        anchor = TemporalAnchor(
            source_id=uuid4(),
            source_generation=1,
            anchor_time=datetime(2025, 3, 15, 10, 0, tzinfo=UTC),
            precision="timed",
        )
        assert anchor.source_generation == 1
        assert anchor.anchor_time.tzinfo == UTC

        with pytest.raises(ValidationError):
            TemporalAnchor(
                source_id=uuid4(),
                source_generation=0,  # ge=1 required
                anchor_time=datetime.now(UTC),
            )

        with pytest.raises(ValidationError, match="Timestamp must be timezone-aware"):
            TemporalAnchor(
                source_id=uuid4(),
                source_generation=1,
                anchor_time=datetime(2025, 3, 15, 10, 0),  # naive rejected  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
            )


class TestEpisodeRequestTemporalAnchoring:
    """Tests for EpisodeRequest reference_time anchoring and timezone awareness."""

    def test_episode_request_rejects_naive_reference_time(self) -> None:
        """Verify EpisodeRequest rejects naive reference_time."""
        naive_time = datetime(2025, 1, 1, 12, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValueError, match="Graph episode is invalid or exceeds its bounds"):
            EpisodeRequest(
                episode_id=uuid4(),
                group_id="group_1",
                name="Test Episode",
                content="Some content",
                reference_time=naive_time,  # naive rejected!
                evidence=(),
                canonical_entity_ids=(),
                mapping_revision=1,
            )

    def test_episode_request_accepts_aware_reference_time(self) -> None:
        """Verify EpisodeRequest accepts timezone-aware reference_time."""
        aware_time = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        req = EpisodeRequest(
            episode_id=uuid4(),
            group_id="group_1",
            name="Test Episode",
            content="Some content",
            reference_time=aware_time,
            evidence=(),
            canonical_entity_ids=(),
            mapping_revision=1,
        )
        assert req.reference_time == aware_time
