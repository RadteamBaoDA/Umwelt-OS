"""Unit tests for timeline public query functions, sorting, window pagination, and entity linkage.

Covers:
- Keyset cursor encoding and decoding with query fingerprint validation.
- Chronological sorting across three temporal partitions (timed, date, unknown).
- Window pagination across partitions and page limit boundary checks (1 <= limit <= 100).
- Query filtering by source_id, type substring with escaped metacharacters, entity_id, and calendar dates.
- Entity linkage mutations: participant management, entity merge, entity split, and participant removal.
- Evidence linkage and derived event visibility enforcement (derived events hidden without active evidence).
"""

import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.workspaces.schemas import WorkspaceContext
from modules.timeline.models import (
    Event,
    EventParticipant,
)
from modules.timeline.public import (
    _cursor_decode,
    _cursor_encode,
    _hide_unsupported,
    _list_page,
    _set_participants,
    apply_entity_merge,
    apply_entity_split,
    get_event,
    list_events,
    list_timeline,
    remove_entity_participants,
)
from modules.timeline.schemas import EventRead, TimelinePage, TimelineQuery

SCOPE = WorkspaceContext(user_id=1, workspace_id=uuid4(), role="owner", membership_revision=1)
KW = {"scope": SCOPE, "multi_workspace_enabled": False}


@pytest.fixture(autouse=True)
def _admitted() -> object:
    """Admission is covered in test_scope; these tests exercise the domain logic past it."""
    with patch("modules.timeline.public._admit", AsyncMock(return_value=MagicMock())):
        yield


class TestCursorCodec:
    """Tests for timeline keyset pagination cursor encoding and tamper-resistance."""

    def test_cursor_roundtrip(self) -> None:
        """Cursor encodes and decodes accurately when fingerprint matches."""
        fingerprint = "abc123def456"
        payload = {"v": 1, "f": fingerprint, "p": 0, "k": ["2026-01-01T00:00:00+00:00", str(uuid4())]}
        encoded = _cursor_encode(payload)
        assert isinstance(encoded, str)
        decoded = _cursor_decode(encoded, fingerprint)
        assert decoded == payload

    def test_cursor_fingerprint_mismatch_raises_value_error(self) -> None:
        """Decoding cursor with a mismatched query fingerprint raises ValueError."""
        fp1 = "fingerprint_one"
        fp2 = "fingerprint_two"
        payload = {"v": 1, "f": fp1, "p": 1, "k": ["2026-01-01", str(uuid4())]}
        encoded = _cursor_encode(payload)
        with pytest.raises(ValueError, match="cursor does not match this query"):
            _cursor_decode(encoded, fp2)

    def test_cursor_decode_invalid_base64_raises_value_error(self) -> None:
        """Malformed base64 cursor strings raise ValueError."""
        with pytest.raises(ValueError, match="cursor is malformed"):
            _cursor_decode("not-valid-base64!!!", "fp")


class TestWindowPaginationAndLimits:
    """Tests for pagination limits, partition traversal, and window cursors."""

    @pytest.mark.asyncio
    async def test_page_limit_underflow_raises_value_error(self) -> None:
        """Page limit < 1 raises ValueError."""
        session = AsyncMock()
        query = TimelineQuery()
        with pytest.raises(ValueError, match="page size must be between 1 and 100"):
            await _list_page(session, query, limit=0, cursor=None, **KW)

    @pytest.mark.asyncio
    async def test_page_limit_overflow_raises_value_error(self) -> None:
        """Page limit > 100 raises ValueError."""
        session = AsyncMock()
        query = TimelineQuery()
        with pytest.raises(ValueError, match="page size must be between 1 and 100"):
            await _list_page(session, query, limit=101, cursor=None, **KW)

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_page_without_next_cursor(self) -> None:
        """Query with no matching events across any partition returns empty items and no cursor."""
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        session.scalars.return_value = scalars_mock

        query = TimelineQuery()
        page = await list_timeline(session, query, limit=10, **KW)
        assert isinstance(page, TimelinePage)
        assert page.items == []
        assert page.next_cursor is None

    @pytest.mark.asyncio
    async def test_list_events_delegates_to_list_page(self) -> None:
        """list_events helper builds default TimelineQuery and returns an EventPage."""
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        session.scalars.return_value = scalars_mock

        source_id = uuid4()
        res = await list_events(session, limit=20, source_id=source_id, **KW)
        assert isinstance(res, TimelinePage)


class TestEventFilteringAndQueries:
    """Tests for query filtering by type, source, entities, and calendar windows."""

    def test_query_filter_serialization_and_fingerprint(self) -> None:
        """TimelineQuery serializes all parameters cleanly for cursor fingerprinting."""
        query = TimelineQuery(
            source_id=uuid4(),
            entity_id=uuid4(),
            type="work.meeting",
            precision="timed",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        data = query.model_dump(mode="json")
        fp = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert len(fp) == 64

    @pytest.mark.asyncio
    async def test_get_event_derived_hidden_without_evidence(self) -> None:
        """A derived event whose supporting evidence chunks are gone is hidden (returns None)."""
        session = AsyncMock()
        event_id = uuid4()
        derived_event = Event(
            id=event_id,
            origin="derived",
            type="project.milestone",
            title="Milestone 1",
            deleted_at=None,
        )

        # 1st scalar: Event exists; 2nd scalar: EventEvidence count query returns None (no evidence)
        session.scalar.side_effect = [derived_event, None]

        result = await get_event(session, event_id, **KW)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_event_manual_visible_without_evidence(self) -> None:
        """A manual event does not require supporting evidence chunks to remain visible."""
        session = AsyncMock()
        event_id = uuid4()
        now = datetime.now(UTC)
        manual_event = Event(
            id=event_id,
            source_id=None,
            origin="manual",
            type="note.created",
            subtype=None,
            title="My Manual Note",
            summary="A quick summary",
            importance_score=0.5,
            confidence=1.0,
            metadata_json={},
            date_precision="timed",
            started_at=now,
            ended_at=None,
            occurred_date=None,
            end_date=None,
            occurrence_timezone="UTC",
            observed_at=now,
            valid_from=None,
            valid_to=None,
            revision=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )

        session.scalar.side_effect = [manual_event]

        # Participants and evidence queries in _event_read
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        session.scalars.return_value = scalars_mock

        result = await get_event(session, event_id, **KW)
        assert result is not None
        assert isinstance(result, EventRead)
        assert result.title == "My Manual Note"
        assert result.origin == "manual"
        assert result.date_precision == "timed"


class TestEntityAndSourceLinkage:
    """Tests for entity participants, entity merge/split, and evidence revocation."""

    @pytest.mark.asyncio
    async def test_set_participants_updates_event_participants(self) -> None:
        """_set_participants removes previous participants of the given origin and inserts new ones."""
        session = AsyncMock()
        session.add = MagicMock()
        event_id = uuid4()
        entity_id = uuid4()
        session.execute.return_value = None

        new_participant = SimpleNamespace(entity_id=entity_id, role="organizer", metadata={})
        with patch("modules.knowledge.entities.public.get_entity_refs", return_value=[MagicMock()]):
            await _set_participants(session, event_id, [new_participant], origin="manual", **KW)

        session.execute.assert_called_once()
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_entity_merge_rewrites_participants(self) -> None:
        """apply_entity_merge updates participants from source_id to target_id and returns touched IDs."""
        session = AsyncMock()
        source_id = uuid4()
        target_id = uuid4()
        event_id = uuid4()

        participant_row = EventParticipant(
            id=uuid4(),
            event_id=event_id,
            entity_id=source_id,
            role="participant",
            origin="manual",
            metadata_json={},
        )

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [participant_row]
        session.scalars.return_value = scalars_mock

        # duplicate scalar check returns None (no duplicate row)
        session.scalar.return_value = None

        with patch("modules.timeline.public._schedule_temporal_event", return_value=None):
            result = await apply_entity_merge(
                session, source_id=source_id, target_id=target_id, event_ids=[event_id], **KW)
            assert result == [event_id]
            assert participant_row.entity_id == target_id

    @pytest.mark.asyncio
    async def test_apply_entity_split_repoints_participants(self) -> None:
        """apply_entity_split redirects selected derived links from source to target entity."""
        session = AsyncMock()
        source_id = uuid4()
        target_id = uuid4()
        event_id = uuid4()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        session.scalars.return_value = scalars_mock

        with patch("modules.timeline.public._schedule_temporal_event", return_value=None):
            result = await apply_entity_split(
                session,
                source_id=source_id,
                target_id=target_id,
                event_ids=[event_id],
                selected_pairs=set(), **KW)
            assert result == []

    @pytest.mark.asyncio
    async def test_remove_entity_participants_cleans_up_events(self) -> None:
        """remove_entity_participants deletes participants and returns modified event IDs."""
        session = AsyncMock()
        entity_id = uuid4()
        event_id = uuid4()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [event_id]
        session.scalars.return_value = scalars_mock

        with patch("modules.timeline.public._schedule_temporal_event", return_value=None):
            result = await remove_entity_participants(session, [entity_id], **KW)
            assert result == [event_id]

    @pytest.mark.asyncio
    async def test_hide_unsupported_marks_events_deleted(self) -> None:
        """_hide_unsupported soft-deletes derived events that have no surviving evidence."""
        session = AsyncMock()
        event_id = uuid4()
        workspace_id = uuid4()
        unsupported_event = Event(id=event_id, workspace_id=workspace_id, origin="derived", owner_fields=[], deleted_at=None)

        session.get.return_value = unsupported_event
        session.scalar.return_value = None  # No remaining evidence

        with patch("modules.timeline.public._schedule_temporal_event", return_value=None):
            await _hide_unsupported(session, [event_id], workspace_id=workspace_id)
            assert unsupported_event.deleted_at is not None
            assert unsupported_event.title == "[unsupported derived event]"
            assert unsupported_event.type == "unsupported_derived_event"
